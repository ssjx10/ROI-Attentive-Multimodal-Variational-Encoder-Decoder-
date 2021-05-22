from functools import partial

import torch
from torch import nn as nn
from torch.nn import functional as F
from torch.autograd import Function



'''
Fusion Block based CBAM
'''
class BasicConv(nn.Module):
    def __init__(self, in_planes, out_planes, kernel_size, stride=1, padding=0, dilation=1, groups=1, relu=True, norm=True, bias=False):
        super(BasicConv, self).__init__()
        self.out_channels = out_planes
        
#         self.relu = nn.ReLU(inplace=True) if relu else None
        self.conv = nn.Conv3d(in_planes, out_planes, kernel_size=kernel_size, stride=stride, padding=padding, dilation=dilation, groups=groups, bias=bias)
        self.norm = nn.InstanceNorm3d(out_planes) if norm else None
        self.relu = nn.LeakyReLU(negative_slope=1e-2, inplace=True) if relu else None

    def forward(self, x):
        
        x = self.conv(x)
        if self.norm is not None:
            x = self.norm(x)
        if self.relu is not None:
            x = self.relu(x)
        
        return x

class Flatten(nn.Module):
    def forward(self, x):
        return x.view(x.size(0), -1)

class ChannelGate(nn.Module):
    def __init__(self, gate_channels, reduction_ratio=16, pool_types=['avg', 'max']):
        super(ChannelGate, self).__init__()
        self.gate_channels = gate_channels
        
        if gate_channels // reduction_ratio >= 2:
            hidden_channels = gate_channels // reduction_ratio
        else:
            hidden_channels = 2
        self.mlp = nn.Sequential(
            Flatten(),
            nn.Linear(gate_channels, hidden_channels),
            nn.ReLU(),
            nn.Linear(hidden_channels, gate_channels)
            )
        self.pool_types = pool_types
        
    def forward(self, x):
            
        channel_att_sum = None
        for pool_type in self.pool_types:
            if pool_type=='avg':
                avg_pool = F.avg_pool3d( x, (x.size(2), x.size(3), x.size(4)), stride=(x.size(2), x.size(3), x.size(4)))
                channel_att_raw = self.mlp( avg_pool )
            elif pool_type=='max':
                max_pool = F.max_pool3d( x, (x.size(2), x.size(3), x.size(4)), stride=(x.size(2), x.size(3), x.size(4)))
                channel_att_raw = self.mlp( max_pool )
            elif pool_type=='lp':
                lp_pool = F.lp_pool3d( x, 2, (x.size(2), x.size(3), x.size(4)), stride=(x.size(2), x.size(3), x.size(4)))
                channel_att_raw = self.mlp( lp_pool )
            elif pool_type=='lse':
                # LSE pool only
                lse_pool = logsumexp_3d(x)
                channel_att_raw = self.mlp( lse_pool )

            if channel_att_sum is None:
                channel_att_sum = channel_att_raw
            else:
                channel_att_sum = channel_att_sum + channel_att_raw

        scale = F.sigmoid(channel_att_sum ).unsqueeze(2).unsqueeze(3).unsqueeze(4).expand_as(x)
#         scale = channel_att_sum.unsqueeze(2).unsqueeze(3).unsqueeze(4).expand_as(x)
        return x*scale
    
class ModalityGate(nn.Module):
    '''
    weight for each modality
    '''
    def __init__(self, in_channels, in_modalities, pool_types=[]):
        super(ModalityGate, self).__init__()
        kernel_size = 1
        self.conv = BasicConv(in_channels, in_modalities, kernel_size, stride=1, padding=(kernel_size-1) // 2, relu=False, norm=False)
#         self.mlp = nn.Sequential(
#             Flatten(),
#             nn.Linear(in_channels, in_modalities),
#             )
        self.in_modalities = in_modalities
        self.mod_channels = in_channels // in_modalities
        self.pool_types = pool_types
        
    def forward(self, x):
        
        att_sum = None
        for pool_type in self.pool_types:
            if pool_type=='avg':
                avg_pool = F.avg_pool3d( x, (x.size(2), x.size(3), x.size(4)), stride=(x.size(2), x.size(3), x.size(4)))
                att_raw = self.mlp( avg_pool )
#                 avg_mod = []
#                 for mod_x in x:
#                     avg_mod.append( torch.mean(mod_x,1).unsqueeze(1) )
#                 avg_mod = torch.cat(avg_mod, 1)
#                 att_raw = self.conv( avg_mod )
            elif pool_type=='max':
                max_pool = F.max_pool3d( x, (x.size(2), x.size(3), x.size(4)), stride=(x.size(2), x.size(3), x.size(4)))
                att_raw = self.mlp( max_pool )
#                 max_mod = []
#                 for mod_x in x:
#                     max_mod.append( torch.max(mod_x,1)[0].unsqueeze(1) )
#                 max_mod = torch.cat(max_mod, 1)
#                 att_raw = self.conv( max_mod )
        
            if att_sum is None:
                att_sum = att_raw
            else:
                att_sum = att_sum + att_raw
        
        att_sum = self.conv(x)
        scale = F.sigmoid(att_sum )
        scaled_x = []
        for i in range(self.in_modalities):
            scaled_x.append(x[:, self.mod_channels*i:self.mod_channels*(i+1)]*scale[:, i:i+1])
            
        return scaled_x

def logsumexp_3d(tensor):
    tensor_flatten = tensor.view(tensor.size(0), tensor.size(1), -1)
    s, _ = torch.max(tensor_flatten, dim=2, keepdim=True)
    outputs = s + (tensor_flatten - s).exp().sum(dim=2, keepdim=True).log()
    return outputs

class ChannelPool(nn.Module):
    def forward(self, x):
        return torch.cat( (torch.max(x,1)[0].unsqueeze(1), torch.mean(x,1).unsqueeze(1)), dim=1 )

class SpatialGate(nn.Module):
    def __init__(self):
        super(SpatialGate, self).__init__()
        kernel_size = 7
        self.compress = ChannelPool()
        self.spatial = BasicConv(2, 1, kernel_size, stride=1, padding=(kernel_size-1) // 2, relu=False)
#         self.spatial2 = BasicConv(2, 1, 3, stride=1, padding=(3-1) // 2, relu=False)
    def forward(self, x):
        x_compress = self.compress(x)
        x_out = self.spatial(x_compress)
#         x_out2 = self.spatial2(x_compress)
        scale = F.sigmoid(x_out) # broadcasting
#         scale = x_out
    
        return x*scale

class CBAM(nn.Module):
    def __init__(self, in_channels, gate_channels=None, comp_ratio=1, reduction_ratio=16, pool_types=['avg', 'max'], no_spatial=False):
        super(CBAM, self).__init__()
        self.ChannelGate_prev = None
        self.SpatialGate_prev = None
        if gate_channels == None:
            if comp_ratio == 1:
                gate_channels = in_channels
                self.compress = None
            else:
                # 1/4
                gate_channels = in_channels
                self.compress = BasicConv(in_channels, in_channels // comp_ratio, 1, stride=1)
                
        else:
            self.compress = BasicConv(in_channels, gate_channels, 1, stride=1)
#             self.SpatialGate_prev = SpatialGate()
#             self.ChannelGate_prev = ChannelGate(in_channels - gate_channels, reduction_ratio, pool_types)
        ############### 확인 gate_channels #######
        self.ChannelGate = ChannelGate(gate_channels, reduction_ratio, pool_types)
        self.no_spatial=no_spatial
        if not no_spatial:
            self.SpatialGate = SpatialGate()
#             self.SpatialGate2 = SpatialGate()
    def forward(self, x, x_prev=None, spa_info=None):
#         max_x = []
#         mean_x = []
#         for mx in x:
#             max_x.append( torch.max(mx,1)[0].unsqueeze(1) )
#             mean_x.append( torch.mean(mx,1).unsqueeze(1) ) 
#         max_x = torch.cat(max_x, 1)
#         mean_x = torch.cat(mean_x, 1)
        if type(x) == list:
            x = torch.cat(x, 1)
        
        x_ch = self.ChannelGate(x)
        
        if self.SpatialGate_prev is not None:
            x_prev = self.SpatialGate_prev(x_prev)
        
        if self.ChannelGate_prev is not None:
#             x = torch.cat([x, x_prev], 1)
            x_prev = self.ChannelGate_prev(x_prev)
            
        if self.compress is not None:
            if x_prev is not None:
                x = torch.cat([x, x_prev], 1)
            x = self.compress(x)
        
        if not self.no_spatial:
            if spa_info is None:
                x_spa = self.SpatialGate(x_ch)
            else:
                x_spa = x_ch*( F.sigmoid(self.SpatialGate(x_ch) + spa_info) )
            
#             x_out = (x_ch + x_spa)
            return x_spa, x_ch
        else:
            
            return x_ch, None

class MCBAM(nn.Module):
    def __init__(self, in_channels, in_modalities=4, pool_types=['avg', 'max'], no_spatial=False):
        super(MCBAM, self).__init__()
        self.ModalityGate = ModalityGate(in_channels, in_modalities)
        self.in_modalities = in_modalities
        self.mod_channels = in_channels // in_modalities
        self.no_spatial=no_spatial
        if not no_spatial:
            self.SpatialGate = SpatialGate()
            
    def forward(self, x, x_prev=None):
        
        x = torch.cat(x, 1)
        x = self.ModalityGate(x)
    
#         if self.compress is not None:
#             if x_prev is not None:
#                 x = torch.cat([x, x_prev], 1)
#             x = self.compress(x)
        
        if not self.no_spatial:
            x = torch.cat(x, 1)
            x_out = self.SpatialGate(x)
            
            # for u-hved
            x_out_list = []
            for i in range(self.in_modalities):
                x_out_list.append(x_out[:, self.mod_channels*i:self.mod_channels*(i+1)])
                
            return x_out_list, x
            
        else:
            return x, None
       
'''
Fusion Block - end -
'''

class ZeroLayerF(Function): # instance missing

    @staticmethod
    def forward(ctx, x, alpha):
        ctx.alpha = alpha
        new_x = x.clone()
        new_x[alpha] = 0
        
        return new_x

    @staticmethod
    def backward(ctx, grad_output):
        zero_grad = grad_output.clone() # need a clone!!
        zero_grad[ctx.alpha] = 0 # drop
        
        return zero_grad, None
    
class ZeroLayerF2(Function): # batch missing

    @staticmethod
    def forward(ctx, x, alpha):
        ctx.alpha = alpha
        new_x = x.clone()
        new_x[:, alpha] = 0
        
        return new_x

    @staticmethod
    def backward(ctx, grad_output):
        zero_grad = grad_output.clone() # need a clone!!
        zero_grad[:, ctx.alpha] = 0 # drop
        
        return zero_grad, None

def discriminator_block(in_filters, out_filters, ks=3, stride=2, double=False, normalization=True):
        """Returns downsampling layers of each discriminator block"""
        
        layers = []
        if double == True:
            layers.append(nn.Conv3d(in_filters, out_filters, ks, stride=stride, padding=1))
            if normalization:
                layers.append(nn.InstanceNorm3d(out_filters))
            layers.append(nn.LeakyReLU(0.2, inplace=True))
            in_filters = out_filters
            
        layers.append(nn.Conv3d(in_filters, out_filters, ks, stride=stride, padding=1))
        if normalization:
            layers.append(nn.InstanceNorm3d(out_filters))
        layers.append(nn.LeakyReLU(0.2, inplace=True))
        
        return layers
    
class FusionBlock(nn.Module):
    """ Block for concat(input, condition)
    """
    
    def __init__(self, in_channels):
        super().__init__()

        
    def forward(self, x):
#         x = torch.cat(x, 1)

        x0 = torch.max(x[0], x[1])
        x1 = torch.max(x[2], x[3])
        x = torch.max(x0, x1)
        return x


def conv3d(in_channels, out_channels, kernel_size, stride, bias, padding):
    return nn.Conv3d(in_channels, out_channels, kernel_size, stride, padding=padding, bias=bias)


def create_conv(in_channels, out_channels, kernel_size, stride, order, num_groups, padding):
    """
    Create a list of modules with together constitute a single conv layer with non-linearity
    and optional batchnorm/groupnorm.
    Args:
        in_channels (int): number of input channels
        out_channels (int): number of output channels
        kernel_size(int or tuple): size of the convolving kernel
        order (string): order of things, e.g.
            'cr' -> conv + ReLU
            'gcr' -> groupnorm + conv + ReLU
            'cl' -> conv + LeakyReLU
            'ce' -> conv + ELU
            'bcr' -> batchnorm + conv + ReLU
            'icl' -> instacnenorm + conv + LeakyReLU
            'cil' -> conv + instacnenorm + LeakyReLU
        num_groups (int): number of groups for the GroupNorm
        padding (int or tuple): add zero-padding added to all three sides of the input
    Return:
        list of tuple (name, module)
    """
    assert 'c' in order, "Conv layer MUST be present"
    assert order[0] not in 'rle', 'Non-linearity cannot be the first operation in the layer'

    modules = []
    for i, char in enumerate(order):
        is_before_conv = i < order.index('c')
        if is_before_conv:
            num_channels = in_channels
        else:
            num_channels = out_channels
            
        if char == 'r':
            modules.append(('ReLU', nn.ReLU(inplace=True)))
        elif char == 'l':
            modules.append(('LeakyReLU', nn.LeakyReLU(negative_slope=1e-2, inplace=True)))
        elif char == 'e':
            modules.append(('ELU', nn.ELU(inplace=True)))
        elif char == 'c':
            # add learnable bias only in the absence of batchnorm/groupnorm
            bias = not ('g' in order or 'b' in order)
            modules.append(('conv', conv3d(in_channels, out_channels, kernel_size, stride, bias, padding=padding)))
        elif char == 'g':
            # use only one group if the given number of groups is greater than the number of channels
            if num_channels < num_groups:
                num_groups = 1

            assert num_channels % num_groups == 0, f'Expected number of channels in input to be divisible by num_groups. num_channels={num_channels}, num_groups={num_groups}'
            modules.append(('groupnorm', nn.GroupNorm(num_groups=num_groups, num_channels=num_channels)))
        elif char == 'i':
            modules.append(('instancenorm', nn.InstanceNorm3d(num_features=num_channels)))
        elif char == 'b':
            modules.append(('batchnorm', nn.BatchNorm3d(num_channels)))
        else:
            raise ValueError(f"Unsupported layer type '{char}'. MUST be one of ['b', 'g', 'r', 'l', 'e', 'c']")

    return modules


class SingleConv(nn.Sequential):
    """
    Basic convolutional module consisting of a Conv3d, non-linearity and optional batchnorm/groupnorm. The order
    of operations can be specified via the `order` parameter
    Args:
        in_channels (int): number of input channels
        out_channels (int): number of output channels
        kernel_size (int or tuple): size of the convolving kernel
        order (string): determines the order of layers, e.g.
            'cr' -> conv + ReLU
            'crg' -> conv + ReLU + groupnorm
            'cl' -> conv + LeakyReLU
            'ce' -> conv + ELU
        num_groups (int): number of groups for the GroupNorm
        padding (int or tuple):
    """

    def __init__(self, in_channels, out_channels, kernel_size=3, stride=1, order='gcr', num_groups=8, padding=1):
        super(SingleConv, self).__init__()

        for name, module in create_conv(in_channels, out_channels, kernel_size, stride, order, num_groups, padding=padding):
            self.add_module(name, module)


class DoubleConv(nn.Sequential):
    """
    A module consisting of two consecutive convolution layers (e.g. BatchNorm3d+ReLU+Conv3d).
    We use (Conv3d+ReLU+GroupNorm3d) by default.
    This can be changed however by providing the 'order' argument, e.g. in order
    to change to Conv3d+BatchNorm3d+ELU use order='cbe'.
    Use padded convolutions to make sure that the output (H_out, W_out) is the same
    as (H_in, W_in), so that you don't have to crop in the decoder path.
    Args:
        in_channels (int): number of input channels
        out_channels (int): number of output channels
        encoder (bool): if True we're in the encoder path, otherwise we're in the decoder
        kernel_size (int or tuple): size of the convolving kernel
        order (string): determines the order of layers, e.g.
            'cr' -> conv + ReLU
            'crg' -> conv + ReLU + groupnorm
            'cl' -> conv + LeakyReLU
            'ce' -> conv + ELU
        num_groups (int): number of groups for the GroupNorm
        padding (int or tuple): add zero-padding added to all three sides of the input
    """

    def __init__(self, in_channels, out_channels, encoder=False, kernel_size=3, pool_stride=1, order='gcr', num_groups=8, padding=1):
        super(DoubleConv, self).__init__()
        if encoder:
            # we're in the encoder path
            conv1_in_channels = in_channels
            conv1_out_channels = out_channels // 2
            if conv1_out_channels < in_channels:
                conv1_out_channels = in_channels
            conv2_in_channels, conv2_out_channels = conv1_out_channels, out_channels
        else:
            # we're in the decoder path, decrease the number of channels in the 1st convolution
            conv1_in_channels, conv1_out_channels = in_channels, out_channels
            conv2_in_channels, conv2_out_channels = out_channels, out_channels

        # conv1
        self.add_module('SingleConv1',
                        SingleConv(conv1_in_channels, conv1_out_channels, kernel_size, 1, order, num_groups,
                                   padding=padding))
        # conv2
        self.add_module('SingleConv2',
                        SingleConv(conv2_in_channels, conv2_out_channels, kernel_size, pool_stride, order, num_groups,
                                   padding=padding))


class ExtResNetBlock(nn.Module):
    """
    Basic UNet block consisting of a SingleConv followed by the residual block.
    The SingleConv takes care of increasing/decreasing the number of channels and also ensures that the number
    of output channels is compatible with the residual block that follows.
    This block can be used instead of standard DoubleConv in the Encoder module.
    
    Casecade : https://arxiv.org/pdf/1810.04008.pdf
    Extenstion Motivated by: https://arxiv.org/pdf/1706.00120.pdf
    Notice we use ELU instead of ReLU (order='cge') and put non-linearity after the groupnorm.
    """

    def __init__(self, in_channels, out_channels, kernel_size=3, pool_stride=1, order='cge', num_groups=8, **kwargs):
        super(ExtResNetBlock, self).__init__()

        # first convolution
        self.conv1 = SingleConv(in_channels, out_channels, kernel_size=kernel_size, order=order, num_groups=num_groups)
        # residual block
        self.conv2 = SingleConv(out_channels, out_channels, kernel_size=kernel_size, stride=pool_stride, order=order, num_groups=num_groups)
        # remove non-linearity from the 3rd convolution since it's going to be applied after adding the residual
#         n_order = order
#         for c in 'rel':
#             n_order = n_order.replace(c, '')
#         self.conv3 = SingleConv(out_channels, out_channels, kernel_size=kernel_size, order=n_order,
#                                 num_groups=num_groups)

        # create non-linearity separately
#         if 'l' in order:
#             self.non_linearity = nn.LeakyReLU(negative_slope=0.1, inplace=True)
#         elif 'e' in order:
#             self.non_linearity = nn.ELU(inplace=True)
#         else:
#             self.non_linearity = nn.ReLU(inplace=True)

    def forward(self, x):
        # apply first convolution and save the output as a residual
        out = self.conv1(x)
        residual = out

        # residual block
        out = self.conv2(out)
#         out = self.conv3(out)

        out += residual
#         out = self.non_linearity(out)

        return out


class Encoder(nn.Module):
    """
    A single module from the encoder path consisting of the optional max
    pooling layer (one may specify the MaxPool kernel_size to be different
    than the standard (2,2,2), e.g. if the volumetric data is anisotropic
    (make sure to use complementary scale_factor in the decoder path) followed by
    a DoubleConv module.
    Args:
        in_channels (int): number of input channels
        out_channels (int): number of output channels
        conv_kernel_size (int or tuple): size of the convolving kernel
        apply_pooling (bool): if True use MaxPool3d before DoubleConv
        pool_kernel_size (int or tuple): the size of the window
        pool_type (str): pooling layer: 'max' or 'avg'
        basic_module(nn.Module): either ResNetBlock or DoubleConv
        conv_layer_order (string): determines the order of layers
            in `DoubleConv` module. See `DoubleConv` for more info.
        num_groups (int): number of groups for the GroupNorm
        padding (int or tuple): add zero-padding added to all three sides of the input
    """

    def __init__(self, in_channels, out_channels, num_block=1, conv_kernel_size=3, apply_pooling=True,
                 pool_kernel_size=2, pool_type='max', basic_module=DoubleConv, conv_layer_order='gcr',
                 num_groups=8, padding=1):
        super(Encoder, self).__init__()
        assert pool_type in ['max', 'conv', 'avg']
        if apply_pooling:
#             self.pooling = nn.Conv3d(in_channels, in_channels, 3, 2, 1)
            if pool_type == 'max':
                self.pooling = nn.MaxPool3d(kernel_size=pool_kernel_size)
            elif pool_type == 'conv':
                self.pooling = nn.Conv3d(in_channels, out_channels, 3, 2, 1)
            else:
                self.pooling = nn.AvgPool3d(kernel_size=pool_kernel_size)
        else:
            self.pooling = None
        
        layers = []
        for i in range(num_block):
            layers.append(basic_module(in_channels, out_channels,
                                             encoder=True,
                                             kernel_size=conv_kernel_size,
                                             order=conv_layer_order,
                                             num_groups=num_groups,
                                             padding=padding))
            in_channels = out_channels
        self.basic_module = nn.Sequential(*layers)

    def forward(self, x):
        if self.pooling is not None:
            x = self.pooling(x)
        x = self.basic_module(x)
        return x


class Decoder(nn.Module):
    """
    A single module for decoder path consisting of the upsampling layer
    (either learned ConvTranspose3d or nearest neighbor interpolation) followed by a basic module (DoubleConv or ExtResNetBlock).
    Args:
        in_channels (int): number of input channels
        out_channels (int): number of output channels
        conv_kernel_size (int or tuple): size of the convolving kernel
        scale_factor (tuple): used as the multiplier for the image H/W/D in
            case of nn.Upsample or as stride in case of ConvTranspose3d, must reverse the MaxPool3d operation
            from the corresponding encoder
        basic_module(nn.Module): either ResNetBlock or DoubleConv
        conv_layer_order (string): determines the order of layers
            in `DoubleConv` module. See `DoubleConv` for more info.
        num_groups (int): number of groups for the GroupNorm
        padding (int or tuple): add zero-padding added to all three sides of the input
    """

    def __init__(self, in_channels, out_channels, conv_kernel_size=3, scale_factor=(2, 2, 2), basic_module=DoubleConv,
                 conv_layer_order='gcr', num_groups=8, mode='trilinear', padding=1):
        super(Decoder, self).__init__()
        if basic_module == DoubleConv:
            # if DoubleConv is the basic_module use interpolation for upsampling and concatenation joining
            self.upsampling = Upsampling(transposed_conv=False, in_channels=in_channels, out_channels=out_channels,
                                         kernel_size=conv_kernel_size, scale_factor=scale_factor, mode=mode)
            # concat joining
            self.joining = partial(self._joining, concat=True)
        else:
            # if basic_module=ExtResNetBlock use transposed convolution upsampling and summation joining
            self.upsampling = Upsampling(transposed_conv=True, in_channels=in_channels, out_channels=out_channels,
                                         kernel_size=conv_kernel_size, scale_factor=scale_factor, mode=mode)
            # sum joining
            self.joining = partial(self._joining, concat=False)
            # adapt the number of in_channels for the ExtResNetBlock
            in_channels = out_channels

        self.basic_module = basic_module(in_channels, out_channels,
                                         encoder=False,
                                         kernel_size=conv_kernel_size,
                                         order=conv_layer_order,
                                         num_groups=num_groups,
                                         padding=padding)

    def forward(self, encoder_features, x, up_size=None):
        x = self.upsampling(encoder_features=encoder_features, x=x, up_size=up_size)
        if encoder_features is not None:
            x = self.joining(encoder_features, x)
        x = self.basic_module(x)
        return x

    @staticmethod
    def _joining(encoder_features, x, concat):
        if concat:
            return torch.cat((encoder_features, x), dim=1)
        else:
            return encoder_features + x


class Upsampling(nn.Module):
    """
    Upsamples a given multi-channel 3D data using either interpolation or learned transposed convolution.
    Args:
        transposed_conv (bool): if True uses ConvTranspose3d for upsampling, otherwise uses interpolation
        in_channels (int): number of input channels for transposed conv
            used only if transposed_conv is True
        out_channels (int): number of output channels for transpose conv
            used only if transposed_conv is True
        kernel_size (int or tuple): size of the convolving kernel
            used only if transposed_conv is True
        scale_factor (int or tuple): stride of the convolution
            used only if transposed_conv is True
        mode (str): algorithm used for upsampling:
            'nearest' | 'linear' | 'bilinear' | 'trilinear' | 'area'. Default: 'nearest'
            used only if transposed_conv is False
    """

    def __init__(self, transposed_conv, in_channels=None, out_channels=None, kernel_size=3,
                 scale_factor=(2, 2, 2), mode='trilinear'):
        super(Upsampling, self).__init__()

        if transposed_conv: # con1 - trilinear for ResUnet
            # make sure that the output size reverses the MaxPool3d from the corresponding encoder
            # (D_out = (D_in − 1) ×  stride[0] − 2 ×  padding[0] +  kernel_size[0] +  output_padding[0])
            self.conv1 = nn.Conv3d(in_channels, out_channels, 1)
            self.upsample = partial(self._interpolate, mode=mode)
#             self.upsample = nn.ConvTranspose3d(out_channels, out_channels, kernel_size=2, stride=scale_factor,
#                                                padding=0)
        else: # trilinear for DoubleConv
            self.conv1 = None
            self.upsample = partial(self._interpolate, mode=mode)

    def forward(self, encoder_features, x, up_size):
        if encoder_features is not None:
            output_size = encoder_features.size()[2:]
        else:
            output_size = up_size
        if self.conv1 is not None:
            x = self.conv1(x)
#         print(output_size)
        
        return self.upsample(x, output_size)


    @staticmethod
    def _interpolate(x, size, mode):
        return F.interpolate(x, size=size, mode=mode)

'''
U-HVED Block
'''
class VAEUp(nn.Module):
    """
    VAE up block
    """

    def __init__(self, in_channels, out_channels, conv_kernel_size=3, scale_factor=(2, 2, 2), basic_module=DoubleConv,
                 conv_layer_order='gcr', num_groups=8, mode='trilinear', padding=1):
        super(VAEUp, self).__init__()
        if basic_module == DoubleConv or basic_module == SingleConv:
            # if DoubleConv is the basic_module use interpolation for upsampling and concatenation joining
            self.upsampling = Upsampling(transposed_conv=False, in_channels=in_channels, out_channels=out_channels,
                                         kernel_size=conv_kernel_size, scale_factor=scale_factor, mode=mode)
        else:
            # if basic_module=ExtResNetBlock use transposed convolution upsampling and summation joining
            self.upsampling = Upsampling(transposed_conv=True, in_channels=in_channels, out_channels=out_channels,
                                         kernel_size=conv_kernel_size, scale_factor=scale_factor, mode=mode)
            # adapt the number of in_channels for the ExtResNetBlock
            in_channels = out_channels

        self.basic_module = basic_module(in_channels, out_channels,
#                                          encoder=False,
                                         kernel_size=conv_kernel_size,
                                         order=conv_layer_order,
                                         num_groups=num_groups,
                                         padding=padding)

    def forward(self, x):
        D, H, W = x[0,0].shape
        x = self.upsampling(None, x=x, up_size=(D*2, H*2, W*2))
        x = self.basic_module(x)
        
        return x
     
class VAEDown(nn.Sequential):
    """
    VAE down block
    input_shape
    (80,80,80) -> (5,5,5)
    (96,96,96) -> (6,6,6)
    (112,112,112) -> (7,7,7)
    (128,128,128) -> (8,8,8)
    """

    def __init__(self, in_channels, out_channels, latent_dims, input_shape=(5,5,5), kernel_size=3, stride=2, order='gcr', num_groups=8, padding=1):
        super(VAEDown, self).__init__()
        
        layers = []
        layers.append(SingleConv(in_channels, out_channels, kernel_size, stride, order, num_groups,
                            padding=conv_padding))
        layers.append(nn.Flatten())
        layers.append(nn.Linear(out_channels*input_shape[0]*input_shape[1]*input_shape[2], 256)) # 5(80), 6(96), 7(112), 8(128)
        layers.append(nn.Linear(256, latent_dims*2)) # 2*latent
        self.add_module('VAEDown', layers)

class ProductOfExperts(nn.Module):
    """ ref : github.com/mhw32/multimodal-vae-public/blob/master/celeba19/model.py
    Return parameters for product of independent experts.
    See https://arxiv.org/pdf/1410.7827.pdf for equations.
    @param mu: M x D for M experts
    @param logvar: M x D for M experts
    """
    def forward(self, mu_list, logvar_list, mod_list, eps=1e-8): # 0 : prior, 1~N : modality
        logvar = [logvar_list[mod+1] for mod in mod_list] + [logvar_list[0]]
        mu = [mu_list[mod+1] for mod in mod_list] + [mu_list[0]]
        logvar = torch.stack(logvar, 0)
        mu = torch.stack(mu, 0)
        
        var       = torch.exp(logvar) + eps
        # precision of i-th Gaussian expert at point x
        T         = 1. / var
        pd_mu     = torch.sum(mu * T, dim=0) / torch.sum(T, dim=0)
        pd_var    = 1. / torch.sum(T, dim=0)
        pd_logvar = torch.log(pd_var)
                
        return pd_mu, pd_logvar
    
class ProductOfExperts2(nn.Module): # for drop
    """ ref : github.com/mhw32/multimodal-vae-public/blob/master/celeba19/model.py
    Return parameters for product of independent experts.
    See https://arxiv.org/pdf/1410.7827.pdf for equations.
    @param mu: M x D for M experts
    @param logvar: M x D for M experts
    """
    def forward(self, mu, logvar, drop, eps=1e-8):
        var       = torch.exp(logvar) + eps
        # precision of i-th Gaussian expert at point x
        T         = 1. / var
        for m in range(drop.shape[1]):
            mu[m+1] = ZeroLayerF.apply(mu[m+1], drop[:, m])
            T[m+1] = ZeroLayerF.apply(T[m+1], drop[:, m])
        pd_mu     = torch.sum(mu * T, dim=0) / torch.sum(T, dim=0)
        pd_var    = 1. / torch.sum(T, dim=0)
        pd_logvar = torch.log(pd_var)
                
        return pd_mu, pd_logvar        

class Reshape(nn.Module):
    def __init__(self, shape):
        super(Reshape, self).__init__()
        '''
        shape = (C,D,H,W)
        '''
        self.shape = shape

    def forward(self, x):
        return x.view(-1, self.shape[0], self.shape[1], self.shape[2], self.shape[3])