#!/usr/bin/env python3
# -*- coding:utf-8 -*-
# Copyright (c) Megvii, Inc. and its affiliates.

import cv2
import numpy as np
from yolox.utils import adjust_box_anns
import math
import random

from yolox.data.datasets.datasets_wrapper import Dataset
from copy import deepcopy

def box_candidates(box1, box2, wh_thr=2, ar_thr=20, area_thr=0.2):
    # box1(4,n), box2(4,n)
    # Compute candidate boxes which include follwing 5 things:
    # box1 before augment, box2 after augment, wh_thr (pixels), aspect_ratio_thr, area_ratio
    w1, h1 = box1[2] - box1[0], box1[3] - box1[1]
    w2, h2 = box2[2] - box2[0], box2[3] - box2[1]
    ar = np.maximum(w2 / (h2 + 1e-16), h2 / (w2 + 1e-16))  # aspect ratio
    return (
        (w2 > wh_thr)
        & (h2 > wh_thr)
        & (w2 * h2 / (w1 * h1 + 1e-16) > area_thr)
        & (ar < ar_thr)
    )  # candidates


def random_perspective(
    img,
    targets=(),
    degrees=10,
    translate=0.1,
    scale=0.1,
    shear=10,
    perspective=0.0,
    border=(0, 0),
):
    # targets = [cls, xyxy]
    height = img.shape[0] + border[0] * 2  # shape(h,w,c)
    width = img.shape[1] + border[1] * 2

    # Center
    C = np.eye(3)
    C[0, 2] = -img.shape[1] / 2  # x translation (pixels)
    C[1, 2] = -img.shape[0] / 2  # y translation (pixels)

    # Rotation and Scale
    R = np.eye(3)
    a = random.uniform(-degrees, degrees)
    # a += random.choice([-180, -90, 0, 90])  # add 90deg rotations to small rotations
    s = random.uniform(scale[0], scale[1])
    # s = 2 ** random.uniform(-scale, scale)
    R[:2] = cv2.getRotationMatrix2D(angle=a, center=(0, 0), scale=s)

    # Shear
    S = np.eye(3)
    S[0, 1] = math.tan(random.uniform(-shear, shear) * math.pi / 180)  # x shear (deg)
    S[1, 0] = math.tan(random.uniform(-shear, shear) * math.pi / 180)  # y shear (deg)

    # Translation
    T = np.eye(3)
    T[0, 2] = (
        random.uniform(0.5 - translate, 0.5 + translate) * width
    )  # x translation (pixels)
    T[1, 2] = (
        random.uniform(0.5 - translate, 0.5 + translate) * height
    )  # y translation (pixels)

    # Combined rotation matrix
    M = T @ S @ R @ C  # order of operations (right to left) is IMPORTANT

    ###########################
    # For Aug out of Mosaic
    # s = 1.
    # M = np.eye(3)
    ###########################

    if (border[0] != 0) or (border[1] != 0) or (M != np.eye(3)).any():  # image changed
        if perspective:
            img = cv2.warpPerspective(
                img, M, dsize=(width, height), borderValue=(114, 114, 114)
            )
        else:  # affine
            img = cv2.warpAffine(
                img, M[:2], dsize=(width, height), borderValue=(114, 114, 114)
            )

    # Transform label coordinates
    n = len(targets)
    if n:
        # warp points
        xy = np.ones((n * 4, 3))
        xy[:, :2] = targets[:, [0, 1, 2, 3, 0, 3, 2, 1]].reshape(
            n * 4, 2
        )  # x1y1, x2y2, x1y2, x2y1
        xy = xy @ M.T  # transform
        if perspective:
            xy = (xy[:, :2] / xy[:, 2:3]).reshape(n, 8)  # rescale
        else:  # affine
            xy = xy[:, :2].reshape(n, 8)

        # create new boxes
        x = xy[:, [0, 2, 4, 6]]
        y = xy[:, [1, 3, 5, 7]]
        xy = np.concatenate((x.min(1), y.min(1), x.max(1), y.max(1))).reshape(4, n).T

        # clip boxes
        xy[:, [0, 2]] = xy[:, [0, 2]].clip(0, width)
        xy[:, [1, 3]] = xy[:, [1, 3]].clip(0, height)

        # filter candidates
        i = box_candidates(box1=targets[:, :4].T * s, box2=xy.T)
        targets = targets[i]
        targets[:, :4] = xy[i]

    return img, targets

def get_mosaic_coordinate(mosaic_image, mosaic_index, xc, yc, w, h, input_h, input_w):
    # TODO update doc
    # index0 to top left part of image
    if mosaic_index == 0:
        x1, y1, x2, y2 = max(xc - w, 0), max(yc - h, 0), xc, yc
        small_coord = w - (x2 - x1), h - (y2 - y1), w, h
    # index1 to top right part of image
    elif mosaic_index == 1:
        x1, y1, x2, y2 = xc, max(yc - h, 0), min(xc + w, input_w * 2), yc
        small_coord = 0, h - (y2 - y1), min(w, x2 - x1), h
    # index2 to bottom left part of image
    elif mosaic_index == 2:
        x1, y1, x2, y2 = max(xc - w, 0), yc, xc, min(input_h * 2, yc + h)
        small_coord = w - (x2 - x1), 0, w, min(y2 - y1, h)
    # index2 to bottom right part of image
    elif mosaic_index == 3:
        x1, y1, x2, y2 = xc, yc, min(xc + w, input_w * 2), min(input_h * 2, yc + h)  # noqa
        small_coord = 0, 0, min(w, x2 - x1), min(y2 - y1, h)
    return (x1, y1, x2, y2), small_coord


class MosaicDetection(Dataset):
    """Detection dataset wrapper that performs mixup for normal dataset."""

    def __init__(
        self, dataset, img_size, mosaic=True, preproc=None,
        degrees=10.0, translate=0.1, scale=(0.5, 1.5), mscale=(0.5, 1.5),
        shear=2.0, perspective=0.0, enable_mixup=True,
        mosaic_prob=1.0, mixup_prob=1.0, *args
    ):
        """

        Args:
            dataset(Dataset) : Pytorch dataset object.
            img_size (tuple):
            mosaic (bool): enable mosaic augmentation or not.
            preproc (func):
            degrees (float):
            translate (float):
            scale (tuple):
            mscale (tuple):
            shear (float):
            perspective (float):
            enable_mixup (bool):
            *args(tuple) : Additional arguments for mixup random sampler.
        """
        super().__init__(img_size, mosaic=mosaic)
        self._dataset = dataset
        self.preproc = preproc
        self.degrees = degrees
        self.translate = translate
        self.scale = scale
        self.shear = shear
        self.perspective = perspective
        self.mixup_scale = mscale
        self.enable_mosaic = mosaic
        self.enable_mixup = enable_mixup
        self.mosaic_prob = mosaic_prob
        self.mixup_prob = mixup_prob

    def __len__(self):
        return len(self._dataset)
    

    # @Dataset.mosaic_getitem
    # def __getitem__(self, idx):
    #     if self.enable_mosaic and random.random() < self.mosaic_prob:
    #         mosaic_labels = []
    #         input_dim = self._dataset.input_dim
    #         input_h, input_w = input_dim[0], input_dim[1]

    #         # yc, xc = s, s  # mosaic center x, y
    #         yc = int(random.uniform(0.5 * input_h, 1.5 * input_h))
    #         xc = int(random.uniform(0.5 * input_w, 1.5 * input_w))

    #         # 3 additional image indices
    #         indices = [idx] + [random.randint(0, len(self._dataset) - 1) for _ in range(3)]

    #         for i_mosaic, index in enumerate(indices):
    #             img, _labels, _, _ = self._dataset.pull_item(index)
    #             h0, w0 = img.shape[:2]  # orig hw
    #             scale = min(1. * input_h / h0, 1. * input_w / w0)
    #             img = cv2.resize(
    #                 img, (int(w0 * scale), int(h0 * scale)), interpolation=cv2.INTER_LINEAR
    #             )
    #             # generate output mosaic image
    #             (h, w, c) = img.shape[:3]
    #             if i_mosaic == 0:
    #                 mosaic_img = np.full((input_h * 2, input_w * 2, c), 114, dtype=np.uint8)

    #             # suffix l means large image, while s means small image in mosaic aug.
    #             (l_x1, l_y1, l_x2, l_y2), (s_x1, s_y1, s_x2, s_y2) = get_mosaic_coordinate(
    #                 mosaic_img, i_mosaic, xc, yc, w, h, input_h, input_w
    #             )

    #             mosaic_img[l_y1:l_y2, l_x1:l_x2] = img[s_y1:s_y2, s_x1:s_x2]
    #             padw, padh = l_x1 - s_x1, l_y1 - s_y1

    #             labels = _labels.copy()
    #             # Normalized xywh to pixel xyxy format
    #             if _labels.size > 0:
    #                 labels[:, 0] = scale * _labels[:, 0] + padw
    #                 labels[:, 1] = scale * _labels[:, 1] + padh
    #                 labels[:, 2] = scale * _labels[:, 2] + padw
    #                 labels[:, 3] = scale * _labels[:, 3] + padh
    #             mosaic_labels.append(labels)

    #         if len(mosaic_labels):
    #             mosaic_labels = np.concatenate(mosaic_labels, 0)
    #             np.clip(mosaic_labels[:, 0], 0, 2 * input_w, out=mosaic_labels[:, 0])
    #             np.clip(mosaic_labels[:, 1], 0, 2 * input_h, out=mosaic_labels[:, 1])
    #             np.clip(mosaic_labels[:, 2], 0, 2 * input_w, out=mosaic_labels[:, 2])
    #             np.clip(mosaic_labels[:, 3], 0, 2 * input_h, out=mosaic_labels[:, 3])

    #         mosaic_img, mosaic_labels = random_perspective(
    #             mosaic_img,
    #             mosaic_labels,
    #             degrees=self.degrees,
    #             translate=self.translate,
    #             scale=self.scale,
    #             shear=self.shear,
    #             perspective=self.perspective,
    #             border=[-input_h // 2, -input_w // 2],
    #         )  # border to remove

    #         # -----------------------------------------------------------------
    #         # CopyPaste: https://arxiv.org/abs/2012.07177
    #         # -----------------------------------------------------------------
    #         if self.enable_mixup and not len(mosaic_labels) == 0 and random.random() < self.mixup_prob:
    #             mosaic_img, mosaic_labels = self.mixup(mosaic_img, mosaic_labels, self.input_dim)
    #         mix_img, padded_labels = self.preproc(mosaic_img, mosaic_labels, self.input_dim)
    #         img_info = (mix_img.shape[1], mix_img.shape[0])

    #         return mix_img, padded_labels, img_info, np.array([idx])

    #     else:
    #         self._dataset._input_dim = self.input_dim
    #         img, support_img, label, support_label, img_info, id_ = self._dataset.pull_item(idx)
    #         img, support_img, label, support_label = self.preproc((img, support_img), (label, support_label), self.input_dim)
    #         return np.concatenate((img, support_img), axis=0), (label, support_label), img_info, id_ 



    @Dataset.mosaic_getitem
    def __getitem__(self, idx):
        #print(self.enable_mosaic)
        if self.enable_mosaic and random.random() < self.mosaic_prob:
            mosaic_labels1 = []
            mosaic_labels2 = []
            input_dim = self._dataset.input_dim
            input_h, input_w = input_dim[0], input_dim[1]

            # yc, xc = s, s  # mosaic center x, y
            yc = int(random.uniform(0.5 * input_h, 1.5 * input_h))
            xc = int(random.uniform(0.5 * input_w, 1.5 * input_w))

            # 3 additional image indices
            indices = [idx] + [random.randint(0, len(self._dataset) - 1) for _ in range(3)]

            for i_mosaic, index in enumerate(indices):
                # print(self._dataset.pull_item(index)[0].shape)
                # print(self._dataset.pull_item(index)[1].shape)
                # print(self._dataset.pull_item(index)[2])
                # print(self._dataset.pull_item(index)[3])
                # print(self._dataset.pull_item(index)[4])
                # print(self._dataset.pull_item(index)[5])
                #img, _labels, _, _, = self._dataset.pull_item(index)
                #img, _, _labels, _, _ ,_ = self._dataset.pull_item(index)
                img, support_image, _labels, _support_labels, _, _ = self._dataset.pull_item(index)
                #img = np.concatenate((img, support_img), axis=0)
                #print(target)
                #print(support_target)
                #_labels = (target,support_target)
                #_labels = np.concatenate((target, support_target))
                #print(_labels)
                h0, w0 = img.shape[:2]  # orig hw
                h1,w1 = support_image.shape[:2]
                scale = min(1. * input_h / h0, 1. * input_w / w0)
                scale1 = min(1. * input_h / h1, 1. * input_w / w1)
                img = cv2.resize(
                    img, (int(w0 * scale), int(h0 * scale)), interpolation=cv2.INTER_LINEAR
                )
                img1 = cv2.resize(
                    support_image, (int(w1 * scale1), int(h1 * scale1)), interpolation=cv2.INTER_LINEAR
                )
                # generate output mosaic image
                (h, w, c) = img.shape[:3]
                (h1, w1, c1) = img1.shape[:3]
                if i_mosaic == 0:
                    mosaic_img1 = np.full((input_h * 2, input_w * 2, c), 114, dtype=np.uint8)
                    mosaic_img2 = np.full((input_h * 2, input_w * 2, c), 114, dtype=np.uint8)

                # suffix l means large image, while s means small image in mosaic aug.
                (l_x1, l_y1, l_x2, l_y2), (s_x1, s_y1, s_x2, s_y2) = get_mosaic_coordinate(
                    mosaic_img1, i_mosaic, xc, yc, w, h, input_h, input_w
                )
                (l1_x1, l1_y1, l1_x2, l1_y2), (s1_x1, s1_y1, s1_x2, s1_y2) = get_mosaic_coordinate(
                    mosaic_img2, i_mosaic, xc, yc, w, h, input_h, input_w
                )
                mosaic_img1[l_y1:l_y2, l_x1:l_x2] = img[s_y1:s_y2, s_x1:s_x2]
                mosaic_img2[l1_y1:l1_y2, l1_x1:l1_x2] = img[s1_y1:s1_y2, s1_x1:s1_x2]
                padw, padh = l_x1 - s_x1, l_y1 - s_y1

                labels1 = _labels.copy()
                labels2 = _support_labels.copy()
                #labels = deepcopy(_labels)
                # Normalized xywh to pixel xyxy format
                if _labels.size > 0:
                #if _labels != ():
                    #for i in range(len(_labels)):
                    labels1[:, 0] = scale * _labels[:, 0] + padw
                    labels1[:, 1] = scale * _labels[:, 1] + padh
                    labels1[:, 2] = scale * _labels[:, 2] + padw
                    labels1[:, 3] = scale * _labels[:, 3] + padh
                if _support_labels.size > 0:
                #if _labels != ():
                    #for i in range(len(_labels)):
                    labels2[:, 0] = scale * _support_labels[:, 0] + padw
                    labels2[:, 1] = scale * _support_labels[:, 1] + padh
                    labels2[:, 2] = scale * _support_labels[:, 2] + padw
                    labels2[:, 3] = scale * _support_labels[:, 3] + padh    
                mosaic_labels1.append(labels1)
                mosaic_labels2.append(labels2)
                #mosaic_labels = [mosaic_labels1, mosaic_labels2]
                #mosaic_img = np.concatenate((mosaic_img1,mosaic_img2),axis =0)
            if len(mosaic_labels1):
                mosaic_labels1 = np.concatenate(mosaic_labels1, 0)
                np.clip(mosaic_labels1[:, 0], 0, 2 * input_w, out=mosaic_labels1[:, 0])
                np.clip(mosaic_labels1[:, 1], 0, 2 * input_h, out=mosaic_labels1[:, 1])
                np.clip(mosaic_labels1[:, 2], 0, 2 * input_w, out=mosaic_labels1[:, 2])
                np.clip(mosaic_labels1[:, 3], 0, 2 * input_h, out=mosaic_labels1[:, 3])
            if len(mosaic_labels2):
                mosaic_labels2 = np.concatenate(mosaic_labels2, 0)
                np.clip(mosaic_labels2[:, 0], 0, 2 * input_w, out=mosaic_labels2[:, 0])
                np.clip(mosaic_labels2[:, 1], 0, 2 * input_h, out=mosaic_labels2[:, 1])
                np.clip(mosaic_labels2[:, 2], 0, 2 * input_w, out=mosaic_labels2[:, 2])
                np.clip(mosaic_labels2[:, 3], 0, 2 * input_h, out=mosaic_labels2[:, 3])
            mosaic_img1, mosaic_labels1 = random_perspective(
                mosaic_img1,
                mosaic_labels1,
                degrees=self.degrees,
                translate=self.translate,
                scale=self.scale,
                shear=self.shear,
                perspective=self.perspective,
                border=[-input_h // 2, -input_w // 2],
            )  # border to remove
            mosaic_img2, mosaic_labels2 = random_perspective(
                mosaic_img2,
                mosaic_labels2,
                degrees=self.degrees,
                translate=self.translate,
                scale=self.scale,
                shear=self.shear,
                perspective=self.perspective,
                border=[-input_h // 2, -input_w // 2],
            ) 
            # -----------------------------------------------------------------
            # CopyPaste: https://arxiv.org/abs/2012.07177
            # -----------------------------------------------------------------
              
            #mosaic_labels = [mosaic_labels1, mosaic_labels2]
            #mosaic_img = np.concatenate((mosaic_img1,mosaic_img2),axis =0)
            if self.enable_mixup and not len(mosaic_labels1) == 0 and random.random() < self.mixup_prob:
                mosaic_img1, mosaic_labels1 = self.mixup(mosaic_img1, mosaic_labels1, self.input_dim)
            if self.enable_mixup and not len(mosaic_labels2) == 0 and random.random() < self.mixup_prob:
                mosaic_img1, mosaic_labels1 = self.mixup(mosaic_img1, mosaic_labels1, self.input_dim)    
            mix_img1, mix_img2, padded_labels1, padded_labels2 = self.preproc((mosaic_img1,mosaic_img2), (mosaic_labels1,mosaic_labels2), self.input_dim)
            #mix_img2, padded_labels2 = self.preproc(mosaic_img2, mosaic_labels2, self.input_dim)
            img_info = (mix_img1.shape[1], mix_img1.shape[0])

            return np.concatenate((mix_img1, mix_img2), axis=0), (padded_labels1, padded_labels2), img_info, np.array([idx])

        else:
            self._dataset._input_dim = self.input_dim
            img, support_img, label, support_label, img_info, id_ = self._dataset.pull_item(idx)
            img, support_img, label, support_label = self.preproc((img, support_img), (label, support_label), self.input_dim)
            return np.concatenate((img, support_img), axis=0), (label, support_label), img_info, id_
    

    # @Dataset.mosaic_getitem
    # def __getitem__(self, idx):
    #     if self.enable_mosaic and random.random() < self.mosaic_prob:
    #         mosaic_labels = []
    #         input_dim = self._dataset.input_dim
    #         input_h, input_w = input_dim[0], input_dim[1]

    #         # yc, xc = s, s  # mosaic center x, y
    #         yc = int(random.uniform(0.5 * input_h, 1.5 * input_h))
    #         xc = int(random.uniform(0.5 * input_w, 1.5 * input_w))

    #         # 3 additional image indices
    #         indices = [idx] + [random.randint(0, len(self._dataset) - 1) for _ in range(3)]

    #         for i_mosaic, index in enumerate(indices):
    #             #img,_, _labels,_, _, _ = self._dataset.pull_item(index)
    #             img,_ , _labels, _ ,  _, _ = self._dataset.pull_item(index)
    #             h0, w0 = img.shape[:2]  # orig hw
    #             scale = min(1. * input_h / h0, 1. * input_w / w0)
    #             img = cv2.resize(
    #                 img, (int(w0 * scale), int(h0 * scale)), interpolation=cv2.INTER_LINEAR
    #             )
    #             # generate output mosaic image
    #             (h, w, c) = img.shape[:3]
    #             if i_mosaic == 0:
    #                 mosaic_img = np.full((input_h * 2, input_w * 2, c), 114, dtype=np.uint8)

    #             # suffix l means large image, while s means small image in mosaic aug.
    #             (l_x1, l_y1, l_x2, l_y2), (s_x1, s_y1, s_x2, s_y2) = get_mosaic_coordinate(
    #                 mosaic_img, i_mosaic, xc, yc, w, h, input_h, input_w
    #             )

    #             mosaic_img[l_y1:l_y2, l_x1:l_x2] = img[s_y1:s_y2, s_x1:s_x2]
    #             padw, padh = l_x1 - s_x1, l_y1 - s_y1

    #             labels = _labels.copy()
    #             # Normalized xywh to pixel xyxy format
    #             if _labels.size > 0:
    #                 labels[:, 0] = scale * _labels[:, 0] + padw
    #                 labels[:, 1] = scale * _labels[:, 1] + padh
    #                 labels[:, 2] = scale * _labels[:, 2] + padw
    #                 labels[:, 3] = scale * _labels[:, 3] + padh
    #             mosaic_labels.append(labels)

    #         if len(mosaic_labels):
    #             mosaic_labels = np.concatenate(mosaic_labels, 0)
    #             np.clip(mosaic_labels[:, 0], 0, 2 * input_w, out=mosaic_labels[:, 0])
    #             np.clip(mosaic_labels[:, 1], 0, 2 * input_h, out=mosaic_labels[:, 1])
    #             np.clip(mosaic_labels[:, 2], 0, 2 * input_w, out=mosaic_labels[:, 2])
    #             np.clip(mosaic_labels[:, 3], 0, 2 * input_h, out=mosaic_labels[:, 3])

    #         mosaic_img, mosaic_labels = random_perspective(
    #             mosaic_img,
    #             mosaic_labels,
    #             degrees=self.degrees,
    #             translate=self.translate,
    #             scale=self.scale,
    #             shear=self.shear,
    #             perspective=self.perspective,
    #             border=[-input_h // 2, -input_w // 2],
    #         )  # border to remove

    #         # -----------------------------------------------------------------
    #         # CopyPaste: https://arxiv.org/abs/2012.07177
    #         # -----------------------------------------------------------------
    #         if self.enable_mixup and not len(mosaic_labels) == 0 and random.random() < self.mixup_prob:
    #             mosaic_img, mosaic_labels = self.mixup(mosaic_img, mosaic_labels, self.input_dim)
    #         mix_img, padded_labels = self.preproc(mosaic_img, mosaic_labels, self.input_dim)
    #         img_info = (mix_img.shape[1], mix_img.shape[0])

    #         return mix_img, padded_labels, img_info, np.array([idx])

    #     else:
    #         self._dataset._input_dim = self.input_dim
    #         img, support_img, label, support_label, img_info, id_ = self._dataset.pull_item(idx)
    #         img, support_img, label, support_label = self.preproc((img, support_img), (label, support_label), self.input_dim)
    #         return np.concatenate((img, support_img), axis=0), (label, support_label), img_info, id_ 
    def mixup(self, origin_img, origin_labels, input_dim):
        jit_factor = random.uniform(*self.mixup_scale)
        FLIP = random.uniform(0, 1) > 0.5
        cp_labels = []
        while len(cp_labels) == 0:
            cp_index = random.randint(0, self.__len__() - 1)
            #print(len(self._dataset.pull_item(cp_index)))
            #_, cp_labels, _, _ = self._dataset.pull_item(cp_index)
            _,_,cp_labels,_,_,_ = self._dataset.pull_item(cp_index)
            #print(cp_labels)
        # print(self._dataset.pull_item(cp_index)[0].shape)
        # print(self._dataset.pull_item(cp_index)[1].shape)
        # print(self._dataset.pull_item(cp_index)[2])
        # print(self._dataset.pull_item(cp_index)[3])
        # print(self._dataset.pull_item(cp_index)[4])
        # print(self._dataset.pull_item(cp_index)[5])   
        img, _ , cp_labels,_,  _, _ = self._dataset.pull_item(cp_index)
        #img, cp_labels, _, _ = self._dataset.pull_item(cp_index)

        if len(img.shape) == 3:
            cp_img = np.ones((input_dim[0], input_dim[1], 3), dtype=np.uint8) * 114
        else:
            cp_img = np.ones(input_dim, dtype=np.uint8) * 114

        cp_scale_ratio = min(input_dim[0] / img.shape[0], input_dim[1] / img.shape[1])
        resized_img = cv2.resize(
            img,
            (int(img.shape[1] * cp_scale_ratio), int(img.shape[0] * cp_scale_ratio)),
            interpolation=cv2.INTER_LINEAR,
        )

        cp_img[
            : int(img.shape[0] * cp_scale_ratio), : int(img.shape[1] * cp_scale_ratio)
        ] = resized_img

        cp_img = cv2.resize(
            cp_img,
            (int(cp_img.shape[1] * jit_factor), int(cp_img.shape[0] * jit_factor)),
        )
        cp_scale_ratio *= jit_factor

        if FLIP:
            cp_img = cp_img[:, ::-1, :]

        origin_h, origin_w = cp_img.shape[:2]
        target_h, target_w = origin_img.shape[:2]
        padded_img = np.zeros(
            (max(origin_h, target_h), max(origin_w, target_w), 3), dtype=np.uint8
        )
        padded_img[:origin_h, :origin_w] = cp_img

        x_offset, y_offset = 0, 0
        if padded_img.shape[0] > target_h:
            y_offset = random.randint(0, padded_img.shape[0] - target_h - 1)
        if padded_img.shape[1] > target_w:
            x_offset = random.randint(0, padded_img.shape[1] - target_w - 1)
        padded_cropped_img = padded_img[
            y_offset: y_offset + target_h, x_offset: x_offset + target_w
        ]

        cp_bboxes_origin_np = adjust_box_anns(
            cp_labels[:, :4].copy(), cp_scale_ratio, 0, 0, origin_w, origin_h
        )
        if FLIP:
            cp_bboxes_origin_np[:, 0::2] = (
                origin_w - cp_bboxes_origin_np[:, 0::2][:, ::-1]
            )
        cp_bboxes_transformed_np = cp_bboxes_origin_np.copy()
        cp_bboxes_transformed_np[:, 0::2] = np.clip(
            cp_bboxes_transformed_np[:, 0::2] - x_offset, 0, target_w
        )
        cp_bboxes_transformed_np[:, 1::2] = np.clip(
            cp_bboxes_transformed_np[:, 1::2] - y_offset, 0, target_h
        )
        keep_list = box_candidates(cp_bboxes_origin_np.T, cp_bboxes_transformed_np.T, 5)

        if keep_list.sum() >= 1.0:
            cls_labels = cp_labels[keep_list, 4:5].copy()
            box_labels = cp_bboxes_transformed_np[keep_list]
            labels = np.hstack((box_labels, cls_labels))
            origin_labels = np.vstack((origin_labels, labels))
            origin_img = origin_img.astype(np.float32)
            origin_img = 0.5 * origin_img + 0.5 * padded_cropped_img.astype(np.float32)

        return origin_img.astype(np.uint8), origin_labels
