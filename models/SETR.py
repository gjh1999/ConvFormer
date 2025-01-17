import torch
import torch.nn as nn
import torch.nn.functional as F
import math
from torch.autograd import Variable
from einops import rearrange, repeat
from einops.layers.torch import Rearrange
import numpy as np
from utils.visualization import attentionheatmap_visual, attentionheatmap_visual2
from models.components.transformer2d_parts import *
from models.components.anti_over_smoothing import Transformer_Reattention, Transformer_Layerscale, Transformer_Refiner, Transformer_Vanilla

def pair(t):
    return t if isinstance(t, tuple) else (t, t)

def relative_pos_dis(height=32, weight=32, sita=0.9):#函数 relative_pos_dis 接受三个参数：
    #height：网格的高度（默认为32）。
#weight：网格的宽度（默认为32）。
#sita：计算中使用的一个参数（默认为0.9）。
    coords_h = torch.arange(height)
    coords_w = torch.arange(weight)
    coords = torch.stack(torch.meshgrid([coords_h, coords_w]))  # 2, Wh, Ww # 0 is 32 * 32 for h, 1 is 32 * 32 for w
    '''
    这部分使用 torch.meshgrid 创建了一个二维坐标网格。得到的 coords 张量的形状为 (2, height, weight)，其中第一个维度表示两个坐标轴，接下来两个维度表示网格的高度和宽度。
    '''
    coords_flatten = torch.flatten(coords, 1)  # 2, Wh*Ww#将坐标展平成形状为 (2, Wh*Ww) 的二维张量。此行计算了展平坐标张量中所有点的相对坐标。结果是一个形状为 (2, WhWw, WhWw) 的张量，其中第一个维度表示坐标轴，接下来两个维度表示成对的相对坐标。
    relative_coords = coords_flatten[:, :, None] - coords_flatten[:, None, :]  # 2, Wh*Ww, 此行计算了展平坐标张量中所有点的相对坐标。结果是一个形状为 (2, WhWw, WhWw) 的张量，其中第一个维度表示坐标轴，接下来两个维度表示成对的相对坐标。
    relative_coords = relative_coords.permute(1, 2, 0).contiguous()  # Wh*Ww, Wh*Ww, 2Wh*Ww将张量的维度置换为 (WhWw, WhWw, 2)，以便更容易处理后续步骤。
    dis = (relative_coords[:, :, 0].float()/height) ** 2 + (relative_coords[:, :, 1].float()/weight) ** 2
    #dis = torch.exp(-dis*(1/(2*sita**2)))
    return  dis

class PreNorm(nn.Module):
    def __init__(self, dim, fn):
        super().__init__()
        self.norm = nn.LayerNorm(dim)
        self.fn = fn
    def forward(self, x, **kwargs):
        return self.fn(self.norm(x), **kwargs)
#归一化层
class CNNPreNorm(nn.Module):
    def __init__(self, dim, fn):
        super().__init__()
        self.norm = nn.BatchNorm2d(dim)
        self.fn = fn
    def forward(self, x, **kwargs):
        return self.fn(self.norm(x), **kwargs)
#cnn归一化层

class FeedForward(nn.Module):
    def __init__(self, dim, hidden_dim, dropout=0.):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(dim, hidden_dim),
            nn.ReLU(inplace=True),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim, dim),
            nn.Dropout(dropout)
        )
    def forward(self, x):
        return self.net(x)
#前向传播层

class CNNFeedForward(nn.Module):
    def __init__(self, dim, hidden_dim, dropout=0.):
        super().__init__()
        self.net = nn.Sequential(
            nn.Conv2d(dim, hidden_dim, kernel_size=1, padding=0, bias=False),
            nn.BatchNorm2d(hidden_dim),
            nn.ReLU(inplace=True),
            nn.Conv2d(hidden_dim, dim, kernel_size=1, padding=0, bias=False),
            nn.BatchNorm2d(dim),
            nn.ReLU(inplace=True)
        )
    def forward(self, x):
        return self.net(x)
#cnn    前向传播层

class Attention(nn.Module):
    def __init__(self, dim, heads=8, dim_head=64, dropout=0.):
        super().__init__()
        inner_dim = dim_head * heads
        project_out = not (heads == 1 and dim_head == dim)

        self.heads = heads
        self.scale = dim_head ** -0.5

        self.attend = nn.Softmax(dim=-1)
        self.to_qkv = nn.Linear(dim, inner_dim * 3, bias = False)

        self.to_out = nn.Sequential(
            nn.Linear(inner_dim, dim),
            nn.Dropout(dropout)
        ) if project_out else nn.Identity()
'''
初始化：

dim: 输入张量的最后一个维度大小。
heads: 多头自注意力机制的头数，默认为8。
dim_head: 每个头的维度，默认为64。
dropout: 用于输出的dropout概率，默认为0。
属性:

inner_dim: 头数和头维度的乘积，表示内部的维度。
project_out: 一个布尔值，表示是否需要将输出投影到原始维度。
self.heads: 多头注意力机制中的头数。
self.scale: 缩放因子，用于调整点积的缩放。
self.attend: 用于计算注意力分布的Softmax层。
self.to_qkv: 将输入投影到查询（q）、键（k）、值（v）的线性层。
self.to_out: 输出层，将注意力机制的结果投影回原始维度。
'''
    def forward(self, x, mode="train"):
        qkv = self.to_qkv(x).chunk(3, dim=-1)
        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h=self.heads), qkv)
        dots = torch.matmul(q, k.transpose(-1, -2)) * self.scale
        dots = dots/1.0 # T
        attn = self.attend(dots)
        #attentionheatmap_visual(attn, out_dir='./Visualization/ACDC/SETR')
        out = torch.matmul(attn, v)
        out = rearrange(out, 'b h n d -> b n (h d)')
        if mode=="train":
            return self.to_out(out)
        else:
            return self.to_out(out), attn
            '''
            前向传播：
将输入 x 通过线性层 self.to_qkv 投影为查询（q）、键（k）、值（v）。
使用 rearrange 函数对查询、键、值进行形状变换，以适应多头自注意力机制。
计算点积得到注意力分数，并通过缩放因子 self.scale 进行缩放。
对得到的点积进行缩放，得到注意力分布。
使用Softmax计算注意力分布。
将注意力分布应用于值，得到加权和。
最后，通过线性层 self.to_out 进行投影，并根据模式选择返回结果。
'''
#注意力层 

class CNNAttention(nn.Module):#卷积注意力机制 加了一个根据距离的权重
    def __init__(self, dim, heads=8, dim_head=64, dropout=0., num_patches=1024):
        super().__init__()
        inner_dim = dim_head * heads
        project_out = not (heads == 1 and dim_head == dim)

        self.heads = heads
        self.scale = dim_head ** -0.5
        self.num_patches = num_patches

        #self.to_qkv = nn.Conv2d(dim, inner_dim * 3, kernel_size=1, padding=0, bias=False)
        self.to_qkv = nn.Conv2d(dim, inner_dim * 3, kernel_size=3, padding=1, bias=False)
        self.dis = relative_pos_dis(math.sqrt(num_patches), math.sqrt(num_patches), sita=0.9).cuda()
        self.headsita = nn.Parameter(torch.randn(heads), requires_grad=True)
        self.sig = nn.Sigmoid()

        self.to_out = nn.Sequential(
            nn.Conv2d(inner_dim, dim, kernel_size=1, padding=0, bias=False),
            nn.BatchNorm2d(dim), # inner_dim
            nn.ReLU(inplace=True),
        ) if project_out else nn.Identity()
'''
dim: 输入张量的通道数。
heads: 多头自注意力机制的头数，默认为8。
dim_head: 每个头的维度，默认为64。
dropout: 用于输出的dropout概率，默认为0。
num_patches: 图像被划分的块数，默认为1024。
属性:

inner_dim: 头数和头维度的乘积，表示内部的维度。
project_out: 一个布尔值，表示是否需要将输出投影到原始维度。
self.heads: 多头注意力机制中的头数。
self.scale: 缩放因子，用于调整点积的缩放。
self.num_patches: 图像被划分的块数。
self.to_qkv: 将输入图像的通道映射到查询（q）、键（k）、值（v）的卷积层。
self.dis: 通过调用 relative_pos_dis 函数计算的相对位置矩阵。
self.headsita: 注意力矩阵的权重，通过Sigmoid激活。
self.sig: Sigmoid激活函数。
self.to_out: 输出层，将注意力机制的结果投影回原始维度。
'''
    def forward(self, x, mode="train", smooth=1e-4):
        qkv = self.to_qkv(x).chunk(3, dim=1)
        q, k, v = map(lambda t: rearrange(t, 'b (g d) h w -> b g (h w) d', g=self.heads), qkv)
        attn = torch.matmul(q, k.transpose(-1, -2)) # b g n n
        qk_norm = torch.sqrt(torch.sum(q ** 2, dim=-1)+smooth)[:, :, :, None] * torch.sqrt(torch.sum(k ** 2, dim=-1)+smooth)[:, :, None, :] + smooth
        attn = attn/qk_norm
        #attentionheatmap_visual2(attn, self.sig(self.headsita), out_dir='./Visualization/ACDC/SETR_plane2', value=1)
        #factor = 1/(2*(self.sig(self.headsita)+0.01)**2) # h
        factor = 1/(2*(self.sig(self.headsita)*(0.4-0.003)+0.003)**2) # af3 + limited setting this, or using the above line code
        dis = factor[:, None, None]*self.dis[None, :, :] # g n n
        dis = torch.exp(-dis)
        dis = dis/torch.sum(dis, dim=-1)[:, :, None]
        #attentionheatmap_visual2(dis[None, :, :, :], self.sig(self.headsita), out_dir='./Visualization/ACDC/dis', value=0.003)
        attn = attn * dis[None, :, :, :]
        #attentionheatmap_visual2(attn, self.sig(self.headsita), out_dir='./Visualization/ACDC/after', value=0.003)
        #attentionheatmap_visual(attn, out_dir='./Visualization/attention_af3/')
        out = torch.matmul(attn, v)
        out = rearrange(out, 'b g (h w) d -> b (g d) h w', h=x.shape[2])
        if mode=="train":
            return self.to_out(out)
        else:
            return self.to_out(out), attn
'''
前向传播：
将输入 x 通过卷积层 self.to_qkv 投影为查询（q）、键（k）、值（v）。
使用 rearrange 函数对查询、键、值进行形状变换，以适应多头自注意力机制。
计算点积得到注意力分数。
对查询和键的范数进行归一化，防止数值不稳定。
计算关注矩阵，并通过相对位置矩阵进行加权。
将注意力矩阵应用于值，得到加权和。
最后，通过卷积层 self.to_out 进行投影，并根据模式选择返回结果。
'''

class Transformer(nn.Module):
    def __init__(self, dim, depth, heads, dim_head, mlp_dim=1024, dropout=0., num_patches=128):
        super().__init__()
        self.layers = nn.ModuleList([])
        for _ in range(depth):
            self.layers.append(nn.ModuleList([
                PreNorm(dim, Attention(dim, heads=heads, dim_head=dim_head, dropout=dropout)),
                PreNorm(dim, FeedForward(dim, mlp_dim, dropout=dropout))
            ]))
    def forward(self, x):
        for attn, ff in self.layers:
            x = attn(x) + x
            x = ff(x) + x
        return x
'''
初始化：

dim: 输入张量的最后一个维度大小。
depth: Transformer模型的层数。
heads: 自注意力机制中的头数。
dim_head: 每个头的维度。
mlp_dim: MLP（多层感知机）中的隐藏层维度，默认为1024。
dropout: 用于注意力机制和MLP的dropout概率，默认为0。
num_patches: 图像被划分的块数，默认为128。
属性:

self.layers: 一个包含Transformer每一层的 nn.ModuleList。
'''

class Transformer_record(nn.Module):#可视化模块 可以记录 
    def __init__(self, dim, depth, heads, dim_head, mlp_dim=1024, dropout=0., num_patches=128):
        super().__init__()
        self.layers = nn.ModuleList([])
        for _ in range(depth):
            self.layers.append(nn.ModuleList([
                PreNorm(dim, Attention(dim, heads=heads, dim_head=dim_head, dropout=dropout)),
                PreNorm(dim, FeedForward(dim, mlp_dim, dropout=dropout))
            ]))
            #与前述 Transformer 类似，包含Transformer每一层的 nn.ModuleList。
    def forward(self, x):
        for attn, ff in self.layers:
            x = attn(x) + x
            x = ff(x) + x
        return x
        '''
        前向传播：
与前述 Transformer 类似，通过每一层的注意力机制（Attention）和MLP（FeedForward）进行多层迭代。
在每一层结束时，将输入与层的输出相加，以便进行残差连接。
'''
    def infere(self, x):
        ftokens, attmaps = [], []
        for attn, ff in self.layers:
            ax, amap = attn(x, mode="record")
            min_ax = torch.min(ax)
            max_ax = torch.max(ax)
            min_x = torch.min(x)
            max_x = torch.max(x)
            print(min_ax.item(), min_x.item(), max_ax.item(), max_x.item())
            x = ax + x
            x = ff(x) + x
            ftokens.append(x)
            attmaps.append(amap)
        return x, ftokens, attmaps
'''
推理：
与前述的前向传播类似，不同之处在于它在每一层注意力机制的输出中记录了注意力矩阵和中间特征张量。
使用 mode="record" 参数调用每一层的注意力机制。
'''

# ================================ components of improved models ================================


class Attention_deepvit(nn.Module):
    def __init__(self, dim, heads=8, dim_head=64, dropout=0.):
        super().__init__()
        inner_dim = dim_head * heads
        project_out = not (heads == 1 and dim_head == dim)

        self.heads = heads
        self.scale = dim_head ** -0.5

        self.attend = nn.Softmax(dim=-1)
        self.to_qkv = nn.Linear(dim, inner_dim * 3, bias = False)

        self.to_out = nn.Sequential(
            nn.Linear(inner_dim, dim),
            nn.Dropout(dropout)
        ) if project_out else nn.Identity()

        self.proj_pre = nn.Linear(self.heads, self.heads, bias=False)
        self.proj_post = nn.Linear(self.heads, self.heads, bias=False)

    def forward(self, x, mode="train"):
        qkv = self.to_qkv(x).chunk(3, dim=-1)
        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h=self.heads), qkv)
        dots = torch.matmul(q, k.transpose(-1, -2)) * self.scale # b h n n
        #dots = self.proj_pre(dots.permute(0,2,3,1)).permute(0,3,1,2) # b h n n -> b n n h
        attn = self.attend(dots)
        attn = self.proj_post(attn.permute(0,2,3,1)).permute(0,3,1,2)
        #attn = self.attend(attn)
        attentionheatmap_visual(attn)
        out = torch.matmul(attn, v)
        out = rearrange(out, 'b h n d -> b n (h d)')
        if mode=="train":
            return self.to_out(out)
        else:
            return self.to_out(out), attn


class Conv2dSamePadding(nn.Conv2d):
    """ 2D Convolutions like TensorFlow """
    def __init__(self, in_channels, out_channels, kernel_size, stride=1, dilation=1, groups=1, bias=True):
        super().__init__(in_channels, out_channels, kernel_size, stride, 0, dilation, groups, bias)
        self.stride = self.stride if len(self.stride) == 2 else [self.stride[0]]*2

    def forward(self, x):
        ih, iw = x.size()[-2:]
        kh, kw = self.weight.size()[-2:]
        sh, sw = self.stride
        oh, ow = math.ceil(ih / sh), math.ceil(iw / sw)
        pad_h = max((oh - 1) * self.stride[0] + (kh - 1) * self.dilation[0] + 1 - ih, 0)
        pad_w = max((ow - 1) * self.stride[1] + (kw - 1) * self.dilation[1] + 1 - iw, 0)
        if pad_h > 0 or pad_w > 0:
            x = F.pad(x, [pad_w//2, pad_w - pad_w//2, pad_h//2, pad_h - pad_h//2])
        return F.conv2d(x, self.weight, self.bias, self.stride, self.padding, self.dilation, self.groups)


class DLA(nn.Module):
    def __init__(self, inp, oup, kernel_size = 3, stride=1, expand_ratio = 3, refine_mode='conv_exapnd'):
        super(DLA, self).__init__()
        """
            Distributed Local Attention used for refining the attention map.
        """

        hidden_dim = round(inp * expand_ratio)
        self.expand_ratio = expand_ratio
        self.identity = stride == 1 and inp == oup
        self.inp, self.oup = inp, oup
        self.high_dim_id = False
        self.refine_mode = refine_mode
        if refine_mode == 'conv':
            self.conv = Conv2dSamePadding(hidden_dim, hidden_dim, (kernel_size,kernel_size), stride, (1,1), groups=1, bias=False)
        elif refine_mode == 'conv_exapnd':
            if self.expand_ratio != 1:
                self.conv_exp = Conv2dSamePadding(inp, hidden_dim, 1, 1, bias=False)
                self.bn1 = nn.BatchNorm2d(hidden_dim)   
            self.depth_sep_conv = Conv2dSamePadding(hidden_dim, hidden_dim, (kernel_size,kernel_size), stride, (1,1), groups=hidden_dim, bias=False)
            self.bn2 = nn.BatchNorm2d(hidden_dim)
            self.conv_pro = Conv2dSamePadding(hidden_dim, oup, 1, 1, bias=False)
            self.bn3 = nn.BatchNorm2d(oup)
            self.relu = nn.ReLU6(inplace=True)

    def forward(self, input):
        x= input
        if self.refine_mode == 'conv':
            return self.conv(x)
        else:
            if self.expand_ratio !=1:
                x = self.relu(self.bn1(self.conv_exp(x)))
            x = self.relu(self.bn2(self.depth_sep_conv(x)))
            x = self.bn3(self.conv_pro(x))
            if self.identity:
                return x + input
            else:
                return x


class Attention_refiner(nn.Module):
    def __init__(self, dim, heads=8, dim_head=64, dropout=0.):
        super().__init__()
        inner_dim = dim_head * heads
        project_out = not (heads == 1 and dim_head == dim)

        self.heads = heads
        self.scale = dim_head ** -0.5

        self.attend = nn.Softmax(dim=-1)
        self.to_qkv = nn.Linear(dim, inner_dim * 3, bias = False)

        self.to_out = nn.Sequential(
            nn.Linear(inner_dim, dim),
            nn.Dropout(dropout)
        ) if project_out else nn.Identity()

        self.DLA = DLA(self.heads, self.heads, kernel_size=3, refine_mode='conv_exapnd', expand_ratio=3)
        self.adapt_bn = nn.BatchNorm2d(self.heads)

    def forward(self, x, mode="train"):
        qkv = self.to_qkv(x).chunk(3, dim=-1)
        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h=self.heads), qkv)
        dots = torch.matmul(q, k.transpose(-1, -2)) * self.scale
        attn = self.attend(dots)
        attn = self.adapt_bn(self.DLA(attn))
        out = torch.matmul(attn, v)
        out = rearrange(out, 'b h n d -> b n (h d)')
        if mode=="train":
            return self.to_out(out)
        else:
            return self.to_out(out), attn


class CNNTransformer_record(nn.Module):
    def __init__(self, dim, depth, heads, dim_head, mlp_dim=1024, dropout=0., num_patches=1024):
        super().__init__()
        self.layers = nn.ModuleList([])
        for _ in range(depth):
            self.layers.append(nn.ModuleList([
                CNNAttention(dim, heads=heads, dim_head=dim_head, dropout=dropout, num_patches=num_patches),
                CNNFeedForward(dim, mlp_dim, dropout=dropout)
            ]))
    def forward(self, x):
        for attn, ff in self.layers:
            x = attn(x) + x
            x = ff(x) + x
        return x
    def infere(self, x):
        ftokens, attmaps = [], []
        for attn, ff in self.layers:
            ax, amap = attn(x, mode="record")
            x = ax + x
            x = ff(x) + x
            ftokens.append(rearrange(x, 'b c h w -> b (h w) c'))
            attmaps.append(amap)
        return x, ftokens, attmaps


class Transformer_deepvit(nn.Module):
    def __init__(self, dim, depth, heads, dim_head, mlp_dim=1024, dropout=0., num_patches=128):
        super().__init__()
        self.layers = nn.ModuleList([])
        for _ in range(depth):
            self.layers.append(nn.ModuleList([
                PreNorm(dim, Attention_deepvit(dim, heads=heads, dim_head=dim_head, dropout=dropout)),
                PreNorm(dim, FeedForward(dim, mlp_dim, dropout=dropout))
            ]))
    def forward(self, x):
        for attn, ff in self.layers:
            x = attn(x) + x
            x = ff(x) + x
        return x
    def infere(self, x):
        ftokens, attmaps = [], []
        for attn, ff in self.layers:
            ax, amap = attn(x, mode="record")
            x = ax + x
            x = ff(x) + x
            ftokens.append(x)
            attmaps.append(amap)
        return x, ftokens, attmaps


class Transformer_cait(nn.Module):
    def __init__(self, dim, depth, heads, dim_head, mlp_dim=1024, dropout=0., num_patches=128):
        super().__init__()
        self.layers = nn.ModuleList([])
        for _ in range(depth):
            self.layers.append(nn.ModuleList([
                PreNorm(dim, Attention(dim, heads=heads, dim_head=dim_head, dropout=dropout)),
                PreNorm(dim, FeedForward(dim, mlp_dim, dropout=dropout))
            ]))
        self.init_values = 1e-4
        self.gamma_1 = nn.Parameter(self.init_values * torch.ones((dim)),requires_grad=True)
        self.gamma_2 = nn.Parameter(self.init_values * torch.ones((dim)),requires_grad=True)
    def forward(self, x):
        for attn, ff in self.layers:
            x = self.gamma_1*attn(x) + x
            x = self.gamma_2*ff(x) + x
        return x
    def infere(self, x):
        ftokens, attmaps = [], []
        for attn, ff in self.layers:
            ax, amap = attn(x, mode="record")
            x = self.gamma_1*ax + x
            x = self.gamma_2*ff(x) + x
            ftokens.append(x)
            attmaps.append(amap)
        return x, ftokens, attmaps


class Transformer_refiner(nn.Module):
    def __init__(self, dim, depth, heads, dim_head, mlp_dim=1024, dropout=0., num_patches=128):
        super().__init__()
        self.layers = nn.ModuleList([])
        for _ in range(depth):
            self.layers.append(nn.ModuleList([
                PreNorm(dim, Attention_refiner(dim, heads=heads, dim_head=dim_head, dropout=dropout)),
                PreNorm(dim, FeedForward(dim, mlp_dim, dropout=dropout))
            ]))
    def forward(self, x):
        for attn, ff in self.layers:
            x = attn(x) + x
            x = ff(x) + x
        return x
    def infere(self, x):
        ftokens, attmaps = [], []
        for attn, ff in self.layers:
            ax, amap = attn(x, mode="record")
            x = ax + x
            x = ff(x) + x
            ftokens.append(x)
            attmaps.append(amap)
        return x, ftokens, attmaps


# ========================================= models =========================================

class Setr(nn.Module):
    def __init__(self, n_channels, n_classes, imgsize, patch_num=32, dim=512, depth=12, heads=8, mlp_dim=512*4, dim_head=64, dropout=0.1, emb_dropout=0.1):
        super().__init__()
        self.image_height, self.image_width = pair(imgsize)
        self.patch_height, self.patch_width = pair(imgsize//patch_num)
        self.dmodel = dim

        assert self.image_height % self.patch_height == 0 and self.image_width % self.patch_width == 0, 'Image dimensions must be divisible by the patch size.'

        num_patches = patch_num * patch_num
        patch_dim = n_channels * self.patch_height * self.patch_width

        self.to_patch_embedding = nn.Sequential(
            Rearrange('b c (h p1) (w p2) -> b (h w) (p1 p2 c)', p1=self.patch_height, p2=self.patch_width),
            nn.Linear(patch_dim, dim),
        )
        self.from_patch_embedding = nn.Sequential(
            Rearrange('b s c -> b c s'),
        )

        self.pos_embedding = nn.Parameter(torch.randn(1, num_patches, dim))
        self.dropout = nn.Dropout(emb_dropout)
        self.transformer = Transformer_record(dim, depth, heads, dim_head, mlp_dim, dropout, num_patches)
        self.decoder = nn.Sequential(
            nn.Conv2d(self.dmodel, self.dmodel // 4, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(self.dmodel // 4),
            nn.ReLU(inplace=True),
            nn.Upsample(scale_factor=2, mode='bilinear', align_corners=True),
            nn.Conv2d(self.dmodel//4, self.dmodel // 4, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(self.dmodel // 4),
            nn.ReLU(inplace=True),
            nn.Upsample(scale_factor=2, mode='bilinear', align_corners=True),
            nn.Conv2d(self.dmodel // 4, n_classes, kernel_size=1),
            nn.Upsample(scale_factor=2, mode='bilinear', align_corners=True),
        )

    def forward(self, img):
        x = self.to_patch_embedding(img)
        b, n, _ = x.shape
        x += self.pos_embedding[:, :n]
        x = self.dropout(x)
        # encoder
        x = self.transformer(x)  # b h*w ppc
        x = self.from_patch_embedding(x)  # b c h*w
        x = x.view(b, self.dmodel, self.image_height//self.patch_height, self.image_width//self.patch_width)
        # decoder
        x = self.decoder(x)
        return x

    def infere(self, img):
        x = self.to_patch_embedding(img)
        b, n, _ = x.shape
        x += self.pos_embedding[:, :n]
        xin = self.dropout(x)
        # encoder
        x, ftokens, attmaps = self.transformer.infere(xin)  # b h*w ppc
        ftokens.insert(0, xin)
        x = self.from_patch_embedding(x)  # b c h*w
        x = x.view(b, self.dmodel, self.image_height//self.patch_height, self.image_width//self.patch_width)
        # decoder
        x = self.decoder(x)
        return x, ftokens, attmaps


class Setr_deepvit(nn.Module):
    def __init__(self, n_channels, n_classes, imgsize, patch_num=32, dim=512, depth=12, heads=8, mlp_dim=512*4, dim_head=64, dropout=0.1, emb_dropout=0.1):
        super().__init__()
        self.image_height, self.image_width = pair(imgsize)
        self.patch_height, self.patch_width = pair(imgsize//patch_num)
        self.dmodel = dim

        assert self.image_height % self.patch_height == 0 and self.image_width % self.patch_width == 0, 'Image dimensions must be divisible by the patch size.'

        num_patches = patch_num * patch_num
        patch_dim = n_channels * self.patch_height * self.patch_width

        self.to_patch_embedding = nn.Sequential(
            Rearrange('b c (h p1) (w p2) -> b (h w) (p1 p2 c)', p1=self.patch_height, p2=self.patch_width),
            nn.Linear(patch_dim, dim),
        )
        self.from_patch_embedding = nn.Sequential(
            Rearrange('b s c -> b c s'),
        )

        self.pos_embedding = nn.Parameter(torch.randn(1, num_patches, dim))
        self.dropout = nn.Dropout(emb_dropout)
        self.transformer = Transformer_deepvit(dim, depth, heads, dim_head, mlp_dim, dropout, num_patches)
        self.decoder = nn.Sequential(
            nn.Conv2d(self.dmodel, self.dmodel // 4, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(self.dmodel // 4),
            nn.ReLU(inplace=True),
            nn.Upsample(scale_factor=2, mode='bilinear', align_corners=True),
            nn.Conv2d(self.dmodel//4, self.dmodel // 4, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(self.dmodel // 4),
            nn.ReLU(inplace=True),
            nn.Upsample(scale_factor=2, mode='bilinear', align_corners=True),
            nn.Conv2d(self.dmodel // 4, n_classes, kernel_size=1),
            nn.Upsample(scale_factor=2, mode='bilinear', align_corners=True),
        )

    def forward(self, img):
        x = self.to_patch_embedding(img)
        b, n, _ = x.shape
        x += self.pos_embedding[:, :n]
        x = self.dropout(x)
        # encoder
        x = self.transformer(x)  # b h*w ppc
        x = self.from_patch_embedding(x)  # b c h*w
        x = x.view(b, self.dmodel, self.image_height//self.patch_height, self.image_width//self.patch_width)
        # decoder
        x = self.decoder(x)
        return x
    def infere(self, img):
        x = self.to_patch_embedding(img)
        b, n, _ = x.shape
        x += self.pos_embedding[:, :n]
        xin = self.dropout(x)
        # encoder
        x, ftokens, attmaps = self.transformer.infere(xin)  # b h*w ppc
        ftokens.insert(0, xin)
        x = self.from_patch_embedding(x)  # b c h*w
        x = x.view(b, self.dmodel, self.image_height//self.patch_height, self.image_width//self.patch_width)
        # decoder
        x = self.decoder(x)
        return x, ftokens, attmaps


class Setr_cait(nn.Module):
    def __init__(self, n_channels, n_classes, imgsize, patch_num=32, dim=512, depth=12, heads=8, mlp_dim=512*4, dim_head=64, dropout=0.1, emb_dropout=0.1):
        super().__init__()
        self.image_height, self.image_width = pair(imgsize)
        self.patch_height, self.patch_width = pair(imgsize//patch_num)
        self.dmodel = dim

        assert self.image_height % self.patch_height == 0 and self.image_width % self.patch_width == 0, 'Image dimensions must be divisible by the patch size.'

        num_patches = patch_num * patch_num
        patch_dim = n_channels * self.patch_height * self.patch_width

        self.to_patch_embedding = nn.Sequential(
            Rearrange('b c (h p1) (w p2) -> b (h w) (p1 p2 c)', p1=self.patch_height, p2=self.patch_width),
            nn.Linear(patch_dim, dim),
        )
        self.from_patch_embedding = nn.Sequential(
            Rearrange('b s c -> b c s'),
        )

        self.pos_embedding = nn.Parameter(torch.randn(1, num_patches, dim))
        self.dropout = nn.Dropout(emb_dropout)
        self.transformer = Transformer_cait(dim, depth, heads, dim_head, mlp_dim, dropout, num_patches)
        self.decoder = nn.Sequential(
            nn.Conv2d(self.dmodel, self.dmodel // 4, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(self.dmodel // 4),
            nn.ReLU(inplace=True),
            nn.Upsample(scale_factor=2, mode='bilinear', align_corners=True),
            nn.Conv2d(self.dmodel//4, self.dmodel // 4, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(self.dmodel // 4),
            nn.ReLU(inplace=True),
            nn.Upsample(scale_factor=2, mode='bilinear', align_corners=True),
            nn.Conv2d(self.dmodel // 4, n_classes, kernel_size=1),
            nn.Upsample(scale_factor=2, mode='bilinear', align_corners=True),
        )

    def forward(self, img):
        x = self.to_patch_embedding(img)
        b, n, _ = x.shape
        x += self.pos_embedding[:, :n]
        x = self.dropout(x)
        # encoder
        x = self.transformer(x)  # b h*w ppc
        x = self.from_patch_embedding(x)  # b c h*w
        x = x.view(b, self.dmodel, self.image_height//self.patch_height, self.image_width//self.patch_width)
        # decoder
        x = self.decoder(x)
        return x
    def infere(self, img):
        x = self.to_patch_embedding(img)
        b, n, _ = x.shape
        x += self.pos_embedding[:, :n]
        xin = self.dropout(x)
        # encoder
        x, ftokens, attmaps = self.transformer.infere(xin)  # b h*w ppc
        ftokens.insert(0, xin)
        x = self.from_patch_embedding(x)  # b c h*w
        x = x.view(b, self.dmodel, self.image_height//self.patch_height, self.image_width//self.patch_width)
        # decoder
        x = self.decoder(x)
        return x, ftokens, attmaps


class Setr_refiner(nn.Module):
    def __init__(self, n_channels, n_classes, imgsize, patch_num=32, dim=512, depth=12, heads=8, mlp_dim=512*4, dim_head=64, dropout=0.1, emb_dropout=0.1):
        super().__init__()
        self.image_height, self.image_width = pair(imgsize)
        self.patch_height, self.patch_width = pair(imgsize//patch_num)
        self.dmodel = dim

        assert self.image_height % self.patch_height == 0 and self.image_width % self.patch_width == 0, 'Image dimensions must be divisible by the patch size.'

        num_patches = patch_num * patch_num
        patch_dim = n_channels * self.patch_height * self.patch_width

        self.to_patch_embedding = nn.Sequential(
            Rearrange('b c (h p1) (w p2) -> b (h w) (p1 p2 c)', p1=self.patch_height, p2=self.patch_width),
            nn.Linear(patch_dim, dim),
        )
        self.from_patch_embedding = nn.Sequential(
            Rearrange('b s c -> b c s'),
        )

        self.pos_embedding = nn.Parameter(torch.randn(1, num_patches, dim))
        self.dropout = nn.Dropout(emb_dropout)
        self.transformer = Transformer_refiner(dim, depth, heads, dim_head, mlp_dim, dropout, num_patches)
        self.decoder = nn.Sequential(
            nn.Conv2d(self.dmodel, self.dmodel // 4, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(self.dmodel // 4),
            nn.ReLU(inplace=True),
            nn.Upsample(scale_factor=2, mode='bilinear', align_corners=True),
            nn.Conv2d(self.dmodel//4, self.dmodel // 4, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(self.dmodel // 4),
            nn.ReLU(inplace=True),
            nn.Upsample(scale_factor=2, mode='bilinear', align_corners=True),
            nn.Conv2d(self.dmodel // 4, n_classes, kernel_size=1),
            nn.Upsample(scale_factor=2, mode='bilinear', align_corners=True),
        )

    def forward(self, img):
        x = self.to_patch_embedding(img)
        b, n, _ = x.shape
        x += self.pos_embedding[:, :n]
        x = self.dropout(x)
        # encoder
        x = self.transformer(x)  # b h*w ppc
        x = self.from_patch_embedding(x)  # b c h*w
        x = x.view(b, self.dmodel, self.image_height//self.patch_height, self.image_width//self.patch_width)
        # decoder
        x = self.decoder(x)
        return x
    def infere(self, img):
        x = self.to_patch_embedding(img)
        b, n, _ = x.shape
        x += self.pos_embedding[:, :n]
        xin = self.dropout(x)
        # encoder
        x, ftokens, attmaps = self.transformer.infere(xin)  # b h*w ppc
        ftokens.insert(0, xin)
        x = self.from_patch_embedding(x)  # b c h*w
        x = x.view(b, self.dmodel, self.image_height//self.patch_height, self.image_width//self.patch_width)
        # decoder
        x = self.decoder(x)
        return x, ftokens, attmaps


class Setr_ConvFormer(nn.Module):
    def __init__(self, n_channels, n_classes, imgsize, patch_num=32, dim=512, depth=12, heads=8, mlp_dim=512*4, dim_head=64, dropout=0.1, emb_dropout=0.1):
        super().__init__()
        self.image_height, self.image_width = pair(imgsize)
        self.patch_height, self.patch_width = pair(imgsize//patch_num)
        self.dmodel = dim

        assert self.image_height % self.patch_height == 0 and self.image_width % self.patch_width == 0, 'Image dimensions must be divisible by the patch size.'

        num_patches = patch_num * patch_num

        self.cnn_encoder = CNNEncoder2(n_channels, dim, self.patch_height, self.patch_width) # the original is CNNs

        self.transformer = CNNTransformer_record(dim, depth, heads, dim_head, mlp_dim, dropout, num_patches)
        self.decoder = nn.Sequential(
            nn.Conv2d(self.dmodel, self.dmodel // 4, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(self.dmodel // 4),
            nn.ReLU(inplace=True),
            nn.Upsample(scale_factor=2, mode='bilinear', align_corners=True),
            nn.Conv2d(self.dmodel//4, self.dmodel // 4, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(self.dmodel // 4),
            nn.ReLU(inplace=True),
            nn.Upsample(scale_factor=2, mode='bilinear', align_corners=True),
            nn.Conv2d(self.dmodel // 4, n_classes, kernel_size=1),
            nn.Upsample(scale_factor=2, mode='bilinear', align_corners=True),
        )

    def forward(self, img):
        x = self.cnn_encoder(img)
        # encoder
        x = self.transformer(x)  # b c h w -> b c h w
        x = self.decoder(x)
        return x
    def infere(self, img):
        x0 = self.cnn_encoder(img)
        # encoder
        x, ftokens, attmaps = self.transformer.infere(x0)
        ftokens.insert(0, rearrange(x0, 'b c h w -> b (h w) c'))
        # decoder
        x = self.decoder(x)
        return x, ftokens, attmaps
