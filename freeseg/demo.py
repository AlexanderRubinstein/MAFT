# Copyright (c) Facebook, Inc. and its affiliates.
from cgitb import text
import logging
import copy
import random
import os
from typing import Tuple
from PIL import Image
import numpy as np

import torch
from torch import nn
from torch.nn import functional as F

from detectron2.config import configurable
from detectron2.data import MetadataCatalog
from detectron2.modeling import META_ARCH_REGISTRY
from detectron2.modeling.backbone import Backbone
from detectron2.modeling.postprocessing import sem_seg_postprocess
from detectron2.structures import Boxes, ImageList, Instances, BitMasks
from detectron2.utils.memory import retry_if_cuda_oom
from detectron2.utils.logger import log_first_n
from detectron2.utils.visualizer import Visualizer, ColorMode
from detectron2.data.transforms import ResizeTransform
from .modeling.clip_adapter import (
    ClipAdapter,
    MaskFormerClipAdapter,
    build_prompt_learner,
)
from .mask_former_model import MaskFormer
from .modeling.clip_adapter.clip import build_clip_model, crop_with_mask, CLIP
from .imagenet_classes import get_in_classes_prompts


@META_ARCH_REGISTRY.register()
class MAFT_DEMO(MaskFormer):

    @configurable
    def __init__(
        self,
        *,
        backbone: Backbone,
        sem_seg_head: nn.Module,
        clip_adapter: nn.Module,
        region_clip_adapter: nn.Module = None,
        criterion: nn.Module,
        num_queries: int,
        semantic_on: bool,
        instance_on: bool,
        panoptic_on: bool,
        object_mask_threshold: float,
        overlap_threshold: float,
        metadata,
        size_divisibility: int,
        sem_seg_postprocess_before_inference: bool,
        clip_ensemble: bool,
        clip_ensemble_weight: float,
        pixel_mean: Tuple[float],
        pixel_std: Tuple[float],
        test_topk_per_image: int,
        cfg,
        clip_pixel_mean,
        clip_pixel_std,
        clip_model_name,
        dis_weight,

    ):
        """
        Args:
            backbone: a backbone module, must follow detectron2's backbone interface
            sem_seg_head: a module that predicts semantic segmentation from backbone features
            criterion: a module that defines the loss
            clip_adapter: adapter for clip-based mask classification
            num_queries: int, number of queries
            panoptic_on: bool, whether to output panoptic segmentation prediction
            object_mask_threshold: float, threshold to filter query based on classification score
                for panoptic segmentation inference
            overlap_threshold: overlap threshold used in general inference for panoptic segmentation
            metadata: dataset meta, get `thing` and `stuff` category names for panoptic
                segmentation inference
            size_divisibility: Some backbones require the input height and width to be divisible by a
                specific integer. We can use this to override such requirement.
            sem_seg_postprocess_before_inference: whether to resize the prediction back
                to original input size before semantic segmentation inference or after.
                For high-resolution dataset like Mapillary, resizing predictions before
                inference will cause OOM error.
            pixel_mean, pixel_std: list or tuple with #channels element, representing
                the per-channel mean and std to be used to normalize the input image
        """
        super().__init__(
            backbone=backbone,
            sem_seg_head=sem_seg_head,
            criterion=criterion,
            num_queries=num_queries,
            semantic_on=semantic_on,
            instance_on=instance_on,
            panoptic_on=panoptic_on,
            object_mask_threshold=object_mask_threshold,
            overlap_threshold=overlap_threshold,
            metadata=metadata,
            size_divisibility=size_divisibility,
            sem_seg_postprocess_before_inference=sem_seg_postprocess_before_inference,
            pixel_mean=pixel_mean,
            pixel_std=pixel_std,
        )
        self.clip_adapter: ClipAdapter = clip_adapter
        self._region_clip_adapter = region_clip_adapter


        self.clip_ensemble: bool = clip_ensemble
        self.clip_ensemble_weight: float = clip_ensemble_weight

        self.test_topk_per_image = test_topk_per_image

        self.register_buffer("clip_pixel_mean", torch.Tensor(clip_pixel_mean).view(-1, 1, 1), False)
        self.register_buffer("clip_pixel_std", torch.Tensor(clip_pixel_std).view(-1, 1, 1), False)
        self.ma_loss = nn.SmoothL1Loss()  # SmoothL1Loss L1Loss L2Loss KLLoss
        self.dis_loss = nn.SmoothL1Loss()
        self.dis_weight = dis_weight

        self.IPCLIP = build_clip_model(clip_model_name, cfg.MODEL.START_LAYERS).visual
        self.T = build_clip_model(clip_model_name).visual

        self._freeze()


    def _freeze(self, ):
        frozen_exclude = ['sem_seg_head', 'backbone', 'clip_adapter', 'T']
        for name, param in self.named_parameters():
            param.requires_grad = True
            if any([exclude in name for exclude in frozen_exclude]):
                param.requires_grad = False
            else:
                assert 'IPCLIP' in name, name
                # clip_adapter
                if not ('transformer.resblocks' in name):
                    param.requires_grad = False
                if 'mlp' in name:
                    param.requires_grad = False
                if name in [
                            'IPCLIP.ln_post.weight', \
                            'IPCLIP.ln_post.bias', \
                            ]:
                    param.requires_grad = True

        for name, param in self.named_parameters():
            if param.requires_grad == True:
                print(name, param.requires_grad)

    @classmethod
    def from_config(cls, cfg):
        init_kwargs = MaskFormer.from_config(cfg)
        prompt_learner = build_prompt_learner(cfg.MODEL.CLIP_ADAPTER)

        clip_adapter = MaskFormerClipAdapter(
            cfg.MODEL.CLIP_ADAPTER.CLIP_MODEL_NAME,
            prompt_learner,
            mask_fill=cfg.MODEL.CLIP_ADAPTER.MASK_FILL,
            mask_expand_ratio=cfg.MODEL.CLIP_ADAPTER.MASK_EXPAND_RATIO,
            mask_thr=cfg.MODEL.CLIP_ADAPTER.MASK_THR,
            mask_matting=cfg.MODEL.CLIP_ADAPTER.MASK_MATTING,
            region_resized=cfg.MODEL.CLIP_ADAPTER.REGION_RESIZED,
        )

        init_kwargs["clip_adapter"] = clip_adapter
        init_kwargs["clip_ensemble"] = cfg.MODEL.CLIP_ADAPTER.CLIP_ENSEMBLE
        init_kwargs[
            "clip_ensemble_weight"
        ] = cfg.MODEL.CLIP_ADAPTER.CLIP_ENSEMBLE_WEIGHT
        init_kwargs["test_topk_per_image"] = cfg.TEST.DETECTIONS_PER_IMAGE
        init_kwargs["metadata"] = MetadataCatalog.get(cfg.DATASETS.TEST[0])
        init_kwargs["semantic_on"] = True

        init_kwargs["cfg"] = cfg

        init_kwargs["clip_model_name"] = cfg.MODEL.CLIP_ADAPTER.CLIP_MODEL_NAME
        init_kwargs["clip_pixel_mean"]=cfg.MODEL.CLIP_PIXEL_MEAN
        init_kwargs["clip_pixel_std"]=cfg.MODEL.CLIP_PIXEL_STD
        init_kwargs["dis_weight"] = cfg.MODEL.dis_weight

        return init_kwargs


    def forward(self, batched_inputs):
        """
        Args:
            batched_inputs: a list, batched outputs of :class:`DatasetMapper`.
                Each item in the list contains the inputs for one image.
                For now, each item in the list is a dict that contains:
                   * "image": Tensor, image in (C, H, W) format.
                   * "instances": per-region ground truth
                   * Other information that's included in the original dicts, such as:
                     "height", "width" (int): the output resolution of the model (may be different
                     from input resolution), used in inference.
        Returns:
            list[dict]:
                each dict has the results for one image. The dict contains the following keys:

                * "sem_seg":
                    A Tensor that represents the
                    per-pixel segmentation prediced by the head.
                    The prediction has shape KxHxW that represents the logits of
                    each class for each pixel.
                * "panoptic_seg":
                    A tuple that represent panoptic output
                    panoptic_seg (Tensor): of shape (height, width) where the values are ids for each segment.
                    segments_info (list[dict]): Describe each segment in `panoptic_seg`.
                        Each dict contains keys "id", "category_id", "isthing".
        """

        dataset_name = "openvocab_dataset"
        class_names = self.get_class_name_list(dataset_name)

        # clip_images
        images = [x["image"].to(self.device) for x in batched_inputs]
        clip_images = [(x - self.clip_pixel_mean) / self.clip_pixel_std for x in images]
        clip_images = ImageList.from_tensors(clip_images, self.size_divisibility)
        clip_images_480 = F.interpolate(clip_images.tensor, size=(480, 480), mode="bilinear", align_corners=False,)

        # resnet_images
        images = [x["image"].to(self.device) for x in batched_inputs]
        images = [(x - self.pixel_mean) / self.pixel_std for x in images]
        images = ImageList.from_tensors(images, self.size_divisibility)


        with torch.no_grad():
            features = self.backbone(images.tensor)
            text_features = self.clip_adapter.get_text_features(class_names, )
            outputs, _ = self.sem_seg_head(features, text_features)

            mask_results = outputs["pred_masks"]
            mask_results = F.interpolate(
                mask_results,
                size=(images.tensor.shape[-2], images.tensor.shape[-1]),
                mode="bilinear",
                align_corners=True,
            ).sigmoid()

        image_features = self.IPCLIP(clip_images_480, mask_results)
        image_features = image_features / image_features.norm(dim=-1, keepdim=True)

        ####
        def load_image(image_path):
            from PIL import Image
            from torchvision.transforms import ToTensor
            image = Image.open(image_path).convert("RGB")  # Convert to RGB format if needed

            # Convert to a PyTorch tensor
            to_tensor = ToTensor()
            tensor = to_tensor(image)

            return tensor

        def save_image(tensor, path):
            import torch
            from torchvision.transforms import ToPILImage
            tensor = tensor * 255
            tensor = tensor.byte()

            # Convert to PIL Image
            to_pil = ToPILImage()
            image = to_pil(tensor)

            # Save the image
            image.save(path)

        in_class_names = get_in_classes_prompts()
        with torch.no_grad():
            in_text_features = self.clip_adapter.get_text_features(in_class_names, )
        idx = 41
        # single_mask = mask_results[0][idx]
        # single_mask_path = "/home/oh/arubinstein17/github/MAFT/masks_saved/41.png" # lemon
        single_mask_path = "/home/oh/arubinstein17/github/MAFT/masks_saved/62.png" # plate
        single_mask = load_image(single_mask_path)[0].cuda()
        single_mask_features = torch.stack([single_mask] * 100).unsqueeze(0)
        single_mask_image_features = self.IPCLIP(clip_images_480, single_mask_features)
        single_mask_image_features = single_mask_image_features / single_mask_image_features.norm(dim=-1, keepdim=True)
        single_mask_clip_cls = self.clip_adapter.get_sim_logits(in_text_features, single_mask_image_features)

        ####

        # CLIP_PIXEL_MEAN:
        #     - 122.7709383
        #     - 116.7460125
        #     - 104.09373615
        # CLIP_PIXEL_STD:
        #     - 68.5005327
        #     - 66.6321579
        #     - 70.323163
        # SIZE_DIVISIBILITY: 32

        wrapped_model = IPClipWrapper(
            self.IPCLIP,
            # text_features,
            text_features=in_text_features,
            clip_pixel_mean=self.clip_pixel_mean,
            clip_pixel_std=self.clip_pixel_std,
            size_divisibility=self.size_divisibility,
            get_sim_logits=self.clip_adapter.get_sim_logits
        )

        images = [x["image"].to(self.device) for x in batched_inputs]
        masks = single_mask.unsqueeze(0)
        single_mask_clip_cls_v2 = wrapped_model(images, masks)

        #####

        clip_cls = self.clip_adapter.get_sim_logits(text_features, image_features)

        if self.training:
            # self distillation loss
            with torch.no_grad():
                image_features_t = self.clip_adapter.get_image_features(clip_images_480, None)
                image_features_t = image_features_t / image_features_t.norm(dim=-1, keepdim=True)
                clip_cls_t = self.clip_adapter.get_sim_logits(text_features, image_features_t) # b*C
            image_features_s = self.IPCLIP(clip_images_480, None)
            image_features_s = image_features_s / image_features_s.norm(dim=-1, keepdim=True)
            clip_cls_s = self.clip_adapter.get_sim_logits(text_features, image_features_s) # b*C
            clip_cls_s = F.softmax(clip_cls_s[...,:-1], dim=-1)
            clip_cls_t = F.softmax(clip_cls_t[...,:-1], dim=-1)
            dis_loss = self.dis_loss(clip_cls_s, clip_cls_t)

            # mask aware loss
            gt_instances = [x["sem_instances"].to(self.device) for x in batched_inputs]
            targets = self.prepare_targets(gt_instances, images)

            clip_cls = clip_cls.squeeze()
            logits_per_image = F.softmax(clip_cls[...,:-1], dim=-1)  # 16*100*156

            logits_per_instance = [] # bn * 100
            labels_per_instance = [] # bn * h*w
            masks_per_instance = []  # bn * 100 * h*w
            assert len(targets)>0, len(targets)
            for b in range(len(targets)):
                maski = mask_results[b].unsqueeze(0)
                for i in range(targets[b]['masks'].shape[0]):
                    logiti = logits_per_image[b,:,targets[b]['labels'][i]].unsqueeze(0)
                    labeli = targets[b]['masks'][i].unsqueeze(0)
                    logits_per_instance.append(logiti)
                    labels_per_instance.append(labeli)
                    masks_per_instance.append(maski)

            masks_per_instance = torch.cat(masks_per_instance, dim = 0)
            labels_per_instance = torch.cat(labels_per_instance, dim = 0)
            logits_per_instance = torch.cat(logits_per_instance, dim = 0)

            ious = self.get_iou(masks_per_instance, labels_per_instance).detach()  # bs*100
            ious = self.mynorm(ious)
            ma_loss = self.ma_loss(logits_per_instance, ious)

            losses = {}
            losses['ma_loss'] = ma_loss
            losses['dis_loss'] = dis_loss * self.dis_weight

            return losses
        else:
            mask_cls_results = outputs["pred_logits"]
            mask_pred_results = outputs["pred_masks"]

            mask_pred_results = F.interpolate(
                mask_pred_results,
                size=(images.tensor.shape[-2], images.tensor.shape[-1]),
                mode="bilinear",
                align_corners=True,
            )

            processed_results = []
            for mask_cls_result, mask_pred_result, input_per_image, image_size, clip_cl in zip(
                mask_cls_results, mask_pred_results, batched_inputs, images.image_sizes, clip_cls
            ):
                height = image_size[0]
                width = image_size[1]
                mask_pred_result = sem_seg_postprocess(
                    mask_pred_result, image_size, height, width
                )

                # semantic segmentation inference
                r = self.semantic_inference(
                    mask_cls_result, mask_pred_result, clip_cl, class_names, dataset_name
                )
                height = input_per_image.get("height", image_size[0])
                width = input_per_image.get("width", image_size[1])
                r = sem_seg_postprocess(r, image_size, height, width)
                processed_results.append({"sem_seg": r})
            return processed_results

    def semantic_inference(self, mask_cls, mask_pred, clip_cl, class_names, dataset_name):
        mask_cls = F.softmax(mask_cls, dim=-1)[..., :-1]
        mask_pred = mask_pred.sigmoid()
        # get the classification result from clip model

        if self.clip_ensemble:

            bin_mask = mask_pred > 0.5
            valid_flag = bin_mask.sum(dim=(-1, -2)) > 0

            clip_cl = clip_cl.squeeze()
            clip_cl = F.softmax(clip_cl[:, :-1], dim=-1)

            if self.clip_ensemble_weight > 0:
                map_back_clip_cls = mask_cls.new_ones(mask_cls.shape)
                map_back_clip_cls[valid_flag] = clip_cl[valid_flag]
                if hasattr(MetadataCatalog.get(dataset_name), "trainable_flag"):
                    trained_mask = torch.Tensor(
                        MetadataCatalog.get(dataset_name).trainable_flag
                    ).to(mask_cls.device)[None, :]
                else:
                    trained_mask = mask_cls.new_zeros(mask_cls.shape)

                mask_cls = trained_mask * torch.pow(
                    mask_cls, self.clip_ensemble_weight
                ) * torch.pow(map_back_clip_cls, 1 - self.clip_ensemble_weight) + (
                    1 - trained_mask
                ) * torch.pow(
                    mask_cls, 1 - self.clip_ensemble_weight
                ) * torch.pow(
                    map_back_clip_cls, self.clip_ensemble_weight
                )
            else:
                mask_cls = clip_cl#[valid_flag]
                mask_pred = mask_pred#[valid_flag]

        semseg = torch.einsum("qc,qhw->chw", mask_cls, mask_pred)

        return semseg

    def get_class_name_list(self, dataset_name):
        class_names = [
            c.strip() for c in MetadataCatalog.get(dataset_name).stuff_classes
        ]
        return class_names


    def get_iou(self, pred, target):
        # pred = pred.sigmoid()
        b, c, h, w = pred.shape
        if len(target.shape)!=len(pred.shape):
            target = target.unsqueeze(1)
        # assert pred.shape == target.shape
        if pred.shape[-2:] != target.shape[-2:]:
            pred = F.interpolate(
            pred,
            size=(target.shape[-2], target.shape[-1]),
            mode="bilinear",
            align_corners=False,
        )


        pred = pred.reshape(b, c,-1)
        target = target.reshape(b, 1, -1)

        #compute the IoU of the foreground
        Iand1 = torch.sum(target*pred, dim = -1)
        Ior1 = torch.sum(target, dim = -1) + torch.sum(pred, dim = -1)-Iand1 + 0.0000001
        IoU1 = Iand1/Ior1

        return IoU1

    def mynorm(self, embeding):
        assert len(embeding.shape) == 2, embeding.shape
        min_em, _ = torch.min(embeding, dim = -1)
        max_em, _ = torch.max(embeding, dim = -1)
        embeding = (embeding-min_em.unsqueeze(-1))/((max_em-min_em+0.00000001).unsqueeze(-1))
        return embeding


    @property
    def region_clip_adapter(self):
        if self._region_clip_adapter is None:
            return self.clip_adapter
        return self._region_clip_adapter


class IPClipWrapper(torch.nn.Module):

    def __init__(
        self,
        inner_clip,
        text_features,
        clip_pixel_mean,
        clip_pixel_std,
        size_divisibility,
        get_sim_logits,
    ):
        super().__init__()
        self.inner_clip = inner_clip
        self.text_features = text_features
        self.clip_pixel_mean = clip_pixel_mean
        self.clip_pixel_std = clip_pixel_std
        self.size_divisibility = size_divisibility
        self.get_sim_logits = get_sim_logits
        # self.tokenizer = tokenizer
        # self.preprocess = preprocess

    def forward(self, images, masks):

        def normalize(features):
            features = features / features.norm(dim=-1, keepdim=True)
            return features

        N_MASKS = 100
        CLIP_IMAGE_SIZE = (480, 480)

        # clip_images
        clip_images = [(x - self.clip_pixel_mean) / self.clip_pixel_std for x in images]
        clip_images = ImageList.from_tensors(clip_images, self.size_divisibility)
        clip_images_480 = F.interpolate(
            clip_images.tensor,
            size=CLIP_IMAGE_SIZE,
            mode="bilinear",
            align_corners=False,
        )

        # repeat mask 100 times
        # masks_100 = torch.stack([masks.unsqueeze(1)] * N_MASKS, dim=1).unsqueeze(0)
        masks_100 = torch.cat([masks] * N_MASKS, dim=0).unsqueeze(0)

        mask_image_features = self.inner_clip(clip_images_480, masks_100)
        mask_image_features = normalize(mask_image_features)
        mask_clip_cls = self.get_sim_logits(self.text_features, mask_image_features)
        return mask_clip_cls[:, 0, ...]
        # return get_text_probs(
        #     self.model,
        #     x,
        #     self.text_features,
        #     subset_tensor=None
        # )


# def get_text_probs(model, image, text_features, subset_tensor):
#     image_features = model.encode_image(image)

#     image_features /= image_features.norm(dim=-1, keepdim=True)
#     text_probs = (100.0 * image_features @ text_features.T)
#     if subset_tensor is not None:
#         text_probs = text_probs * subset_tensor
#     return text_probs
