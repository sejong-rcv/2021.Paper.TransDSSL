import torch
import torch.nn as nn
import torch.nn.functional as F

from .base_model import BaseModel
from .blocks import (
    FeatureFusionBlock,
    FeatureFusionBlock_custom,
    SoftAttDepth,
    Interpolate,
    _make_encoder,
    _make_scratch,
    forward_vit,
)

from dpt.swintf import SwinTransformer

def _make_fusion_block(features, use_bn):
    return FeatureFusionBlock_custom(
        features,
        nn.ReLU(False),
        deconv=False,
        bn=use_bn,
        expand=False,
        align_corners=True,
    )


class DPT(BaseModel):
    def __init__(
        self,
        head,
        features=256,
        backbone="vitb_rn50_384",
        readout="project",
        channels_last=False,
        use_bn=False,
        enable_attention_hooks=False,
    ):

        super(DPT, self).__init__()

        self.channels_last = channels_last

        hooks = {
            "vitb_rn50_384": [0, 1, 8, 11],
            "vitb16_384": [2, 5, 8, 11],
            "vitl16_384": [5, 11, 17, 23],
        }
        self.scratch = _make_scratch(
            [128, 256, 512, 1024], 256, groups=1, expand=False
        ) 
        # Instantiate backbone and reassemble blocks
        self.Swin=SwinTransformer(embed_dim=128, \
                                depths=[2, 2, 18, 2], \
                                num_heads=[4, 8, 16, 32], \
                                window_size=7, \
                                ape=False, \
                                drop_path_rate=0.3, \
                                patch_norm=True, \
                                use_checkpoint=False)
        ch=torch.load("upernet_swin_base_patch4_window7_512x512.pth")
        # import pdb;pdb.set_trace()
        st=self.Swin.state_dict()
        st_keys=list(st.keys())
        keys=ch["state_dict"].keys()
        ch_keys=[]
        for i in keys:
            if "backbone" in i:
                ch_keys.append(i)

        # import pdb;pdb.set_trace()
        for i in range(len(ch_keys)):
            st[st_keys[i]]=ch["state_dict"][ch_keys[i]]
        print("################### Load State_dict [Swin_transformer] ##############################")
        self.Swin.load_state_dict(st)

        self.scratch.refinenet1 = _make_fusion_block(features, use_bn)
        self.scratch.refinenet2 = _make_fusion_block(features, use_bn)
        self.scratch.refinenet3 = _make_fusion_block(features, use_bn)
        self.scratch.refinenet4 = _make_fusion_block(features, use_bn)
        # self.attn_depth=SoftAttDepth()

        # self.scratch.output_conv4 =nn.Sequential(
        #     nn.Conv2d(features, features // 2, kernel_size=3, stride=1, padding=1),
        #     nn.Conv2d(features // 2, 32, kernel_size=3, stride=1, padding=1),
        # )
        # self.scratch.output_conv3 = nn.Sequential(
        #     nn.Conv2d(features, features // 2, kernel_size=3, stride=1, padding=1),
        #     nn.Conv2d(features // 2, 32, kernel_size=3, stride=1, padding=1),
        # )
        # self.scratch.output_conv2 = nn.Sequential(
        #     nn.Conv2d(features, features // 2, kernel_size=3, stride=1, padding=1),
        #     nn.Conv2d(features // 2, 32, kernel_size=3, stride=1, padding=1),
        # )
        self.scratch.output_conv = head
    def normalinput(self, x):
        mean=torch.tensor((123.675/255., 116.28/255., 103.53/255.)).cuda()
        std=torch.tensor((58.395/255., 57.12/255., 57.375/255.)).cuda()
        mean = mean.view(-1, 1, 1)
        std = std.view(-1, 1, 1)
        for i in range(x.shape[0]):
            x[i]=x[i].sub_(mean).div_(std)
        
        return x
    def forward(self, x):
#         if self.channels_last == True:
#             x.contiguous(memory_format=torch.channels_last)
        # import pdb;pdb.set_trace()
        # x = (x - 0.45) / 0.225
        # x=self.normalinput(x)
        layer_1, layer_2, layer_3, layer_4 = self.Swin( x)
        
        layer_1_rn = self.scratch.layer1_rn(layer_1)
        layer_2_rn = self.scratch.layer2_rn(layer_2)
        layer_3_rn = self.scratch.layer3_rn(layer_3)
        layer_4_rn = self.scratch.layer4_rn(layer_4)

        path_4 = self.scratch.refinenet4(layer_4_rn)
        path_3 = self.scratch.refinenet3(path_4, layer_3_rn)
        path_2 = self.scratch.refinenet2(path_3, layer_2_rn)
        path_1 = self.scratch.refinenet1(path_2, layer_1_rn)

        out = self.scratch.output_conv(path_1)

        return [out]


class DPTDepthModel(DPT):
    def __init__(
        self, path=None, non_negative=True, scale=1.0, shift=0.0, invert=False, **kwargs
    ):
        features = kwargs["features"] if "features" in kwargs else 256

        self.scale = scale
        self.shift = shift
        self.invert = invert

        head = nn.Sequential(
            nn.Conv2d(features, features // 2, kernel_size=3, stride=1, padding=1),
            Interpolate(scale_factor=2, mode="bilinear", align_corners=True),
            nn.Conv2d(features // 2, 32, kernel_size=3, stride=1, padding=1),
            nn.ReLU(True),
            nn.Conv2d(32, 1, kernel_size=1, stride=1, padding=0),
            nn.ReLU(True) if non_negative else nn.Identity(),
            nn.Identity(),
        )

        super().__init__(head, **kwargs)

        if path is not None:
            self.load(path)

    def forward(self, rgb):
        # import pdb;pdb.set_trace()
        x=rgb
        # x = (x - 0.45) / 0.225
        inv_depth = super().forward(x)#.squeeze(dim=1)

        if self.invert:
            depth = self.scale * inv_depth + self.shift
            depth[depth < 1e-8] = 1e-8
            depth = 1.0 / depth
            return depth
        else:
            output={}
            output["inv_depths"]=inv_depth
            # import pdb;pdb.set_trace()
        
            return output


class DPTSegmentationModel(DPT):
    def __init__(self, num_classes, path=None, **kwargs):

        features = kwargs["features"] if "features" in kwargs else 256

        kwargs["use_bn"] = True

        head = nn.Sequential(
            nn.Conv2d(features, features, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(features),
            nn.ReLU(True),
            nn.Dropout(0.1, False),
            nn.Conv2d(features, num_classes, kernel_size=1),
            Interpolate(scale_factor=2, mode="bilinear", align_corners=True),
        )

        super().__init__(head, **kwargs)

        self.auxlayer = nn.Sequential(
            nn.Conv2d(features, features, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(features),
            nn.ReLU(True),
            nn.Dropout(0.1, False),
            nn.Conv2d(features, num_classes, kernel_size=1),
        )

        if path is not None:
            self.load(path)
