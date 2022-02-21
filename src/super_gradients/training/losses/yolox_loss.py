"""
Based on https://github.com/Megvii-BaseDetection/YOLOX
"""

import logging
from typing import List, Tuple

import torch
from torch import nn
from torch.nn.modules.loss import _Loss
import torch.nn.functional as F


class IOUloss(nn.Module):
    def __init__(self, reduction="none", loss_type="iou"):
        super(IOUloss, self).__init__()
        self.reduction = reduction
        self.loss_type = loss_type

    def forward(self, pred, target):
        assert pred.shape[0] == target.shape[0]

        pred = pred.view(-1, 4)
        target = target.view(-1, 4)
        tl = torch.max((pred[:, :2] - pred[:, 2:] / 2), (target[:, :2] - target[:, 2:] / 2))
        br = torch.min((pred[:, :2] + pred[:, 2:] / 2), (target[:, :2] + target[:, 2:] / 2))

        area_p = torch.prod(pred[:, 2:], 1)
        area_g = torch.prod(target[:, 2:], 1)

        en = (tl < br).type(tl.type()).prod(dim=1)
        area_i = torch.prod(br - tl, 1) * en
        area_u = area_p + area_g - area_i
        iou = (area_i) / (area_u + 1e-16)

        if self.loss_type == "iou":
            loss = 1 - iou ** 2
        elif self.loss_type == "giou":
            c_tl = torch.min((pred[:, :2] - pred[:, 2:] / 2), (target[:, :2] - target[:, 2:] / 2))
            c_br = torch.max((pred[:, :2] + pred[:, 2:] / 2), (target[:, :2] + target[:, 2:] / 2))
            area_c = torch.prod(c_br - c_tl, 1)
            giou = iou - (area_c - area_u) / area_c.clamp(1e-16)
            loss = 1 - giou.clamp(min=-1.0, max=1.0)

        if self.reduction == "mean":
            loss = loss.mean()
        elif self.reduction == "sum":
            loss = loss.sum()

        return loss


def bboxes_iou(bboxes_a, bboxes_b, xyxy=True):
    if bboxes_a.shape[1] != 4 or bboxes_b.shape[1] != 4:
        raise IndexError

    if xyxy:
        tl = torch.max(bboxes_a[:, None, :2], bboxes_b[:, :2])
        br = torch.min(bboxes_a[:, None, 2:], bboxes_b[:, 2:])
        area_a = torch.prod(bboxes_a[:, 2:] - bboxes_a[:, :2], 1)
        area_b = torch.prod(bboxes_b[:, 2:] - bboxes_b[:, :2], 1)
    else:
        tl = torch.max((bboxes_a[:, None, :2] - bboxes_a[:, None, 2:] / 2), (bboxes_b[:, :2] - bboxes_b[:, 2:] / 2))
        br = torch.min((bboxes_a[:, None, :2] + bboxes_a[:, None, 2:] / 2), (bboxes_b[:, :2] + bboxes_b[:, 2:] / 2))

        area_a = torch.prod(bboxes_a[:, 2:], 1)
        area_b = torch.prod(bboxes_b[:, 2:], 1)

    en = (tl < br).type(tl.type()).prod(dim=2)
    area_i = torch.prod(br - tl, 2) * en  # * ((tl < br).all())
    return area_i / (area_a[:, None] + area_b - area_i)


class YoloXDetectionLoss(_Loss):
    """
    Calculate YOLO V5 loss:
    L = L_objectivness + L_iou + L_classification (+ L_l1)
    """

    def __init__(self, strides, im_size, num_classes, use_l1=False, center_sampling_radius=2.5):
        super().__init__()
        self.grids = [torch.zeros(1)] * len(strides)
        self.strides = strides
        self.im_size = im_size
        self.num_classes = num_classes

        self.center_sampling_radius = center_sampling_radius
        self.use_l1 = use_l1
        self.l1_loss = nn.L1Loss(reduction="none")
        self.bcewithlog_loss = nn.BCEWithLogitsLoss(reduction="none")
        self.iou_loss = IOUloss(reduction="none")

    def forward(self, model_output, targets):
        if isinstance(model_output, tuple) and len(model_output) == 2:
            # in test/eval mode the Yolo v5 model output a tuple where the second item is the raw predictions
            _, predictions = model_output
        else:
            predictions = model_output

        return self.compute_loss(predictions, targets)

    @staticmethod
    def _make_grid(nx=20, ny=20):
        yv, xv = torch.meshgrid([torch.arange(ny), torch.arange(nx)])
        return torch.stack((xv, yv), 2).view((1, 1, ny, nx, 2)).float()

    def compute_loss(self, predictions: List[torch.Tensor], targets: torch.Tensor) \
            -> Tuple[torch.Tensor, torch.Tensor]:
        x_shifts, y_shifts, expanded_strides, transformed_outputs, raw_outputs = self.prepare_predictions(predictions)

        bbox_preds = transformed_outputs[:, :, :4]  # [batch, n_anchors_all, 4]
        obj_preds = transformed_outputs[:, :, 4:5]  # [batch, n_anchors_all, 1]
        cls_preds = transformed_outputs[:, :, 5:]  # [batch, n_anchors_all, n_cls]

        # calculate targets
        total_num_anchors = transformed_outputs.shape[1]
        cls_targets = []
        reg_targets = []
        l1_targets = []
        obj_targets = []
        fg_masks = []

        num_fg, num_gts = 0., 0.

        for batch_idx in range(transformed_outputs.shape[0]):
            labels_im = targets[targets[:, 0] == batch_idx]
            num_gt = labels_im.shape[0]
            num_gts += num_gt
            if num_gt == 0:
                cls_target = transformed_outputs.new_zeros((0, self.num_classes))
                reg_target = transformed_outputs.new_zeros((0, 4))
                l1_target = transformed_outputs.new_zeros((0, 4))
                obj_target = transformed_outputs.new_zeros((total_num_anchors, 1))
                fg_mask = transformed_outputs.new_zeros(total_num_anchors).bool()
            else:
                gt_bboxes_per_image = labels_im[:, 2:6] * self.im_size
                gt_classes = labels_im[:, 1]
                bboxes_preds_per_image = bbox_preds[batch_idx]

                try:
                    gt_matched_classes, fg_mask, pred_ious_this_matching, matched_gt_inds, num_fg_img = \
                        self.get_assignments(batch_idx, num_gt, total_num_anchors, gt_bboxes_per_image,
                                             gt_classes, bboxes_preds_per_image,
                                             expanded_strides, x_shifts, y_shifts, cls_preds, obj_preds)
                except RuntimeError:
                    logging.error("OOM RuntimeError is raised due to the huge memory cost during label assignment. \
                                   CPU mode is applied in this batch. If you want to avoid this issue, \
                                   try to reduce the batch size or image size.")
                    torch.cuda.empty_cache()
                    gt_matched_classes, fg_mask, pred_ious_this_matching, matched_gt_inds, num_fg_img = \
                        self.get_assignments(batch_idx, num_gt, total_num_anchors, gt_bboxes_per_image,
                                             gt_classes, bboxes_preds_per_image,
                                             expanded_strides, x_shifts, y_shifts, cls_preds, obj_preds, 'cpu')

                torch.cuda.empty_cache()
                num_fg += num_fg_img

                cls_target = F.one_hot(gt_matched_classes.to(torch.int64), self.num_classes) * \
                             pred_ious_this_matching.unsqueeze(-1)
                obj_target = fg_mask.unsqueeze(-1)
                reg_target = gt_bboxes_per_image[matched_gt_inds]
                if self.use_l1:
                    l1_target = self.get_l1_target(transformed_outputs.new_zeros((num_fg_img, 4)),
                                                   gt_bboxes_per_image[matched_gt_inds], expanded_strides[0][fg_mask],
                                                   x_shifts=x_shifts[0][fg_mask], y_shifts=y_shifts[0][fg_mask])

            cls_targets.append(cls_target)
            reg_targets.append(reg_target)
            obj_targets.append(obj_target.to(transformed_outputs.dtype))
            fg_masks.append(fg_mask)
            if self.use_l1:
                l1_targets.append(l1_target)

        cls_targets = torch.cat(cls_targets, 0)
        reg_targets = torch.cat(reg_targets, 0)
        obj_targets = torch.cat(obj_targets, 0)
        fg_masks = torch.cat(fg_masks, 0)
        if self.use_l1:
            l1_targets = torch.cat(l1_targets, 0)

        num_fg = max(num_fg, 1)
        loss_iou = self.iou_loss(bbox_preds.view(-1, 4)[fg_masks], reg_targets).sum() / num_fg
        loss_obj = self.bcewithlog_loss(obj_preds.view(-1, 1), obj_targets).sum() / num_fg
        loss_cls = self.bcewithlog_loss(cls_preds.view(-1, self.num_classes)[fg_masks], cls_targets).sum() / num_fg
        if self.use_l1:
            loss_l1 = self.l1_loss(raw_outputs.view(-1, 4)[fg_masks], l1_targets).sum() / num_fg
        else:
            loss_l1 = 0.0

        reg_weight = 5.0
        loss = reg_weight * loss_iou + loss_obj + loss_cls + loss_l1

        return loss, torch.cat((loss_iou.unsqueeze(0), loss_obj.unsqueeze(0), loss_cls.unsqueeze(0),
                                torch.tensor(loss_l1).unsqueeze(0).cuda(),
                                torch.tensor(num_fg / max(num_gts, 1)).unsqueeze(0).cuda(),
                                loss.unsqueeze(0))).detach()

    def prepare_predictions(self, predictions: List[torch.Tensor]) -> \
            Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        """
        Convert raw outputs of the network into a format that merges outputs from all levels
        :return:    5 tensors representing predictions:
                        * x_shifts: shape [1 x (num_anchors * num_cells) x 1],
                          where num_cells = grid1X * grid1Y + grid2X * grid2Y + grid3X * grid3Y,
                          x coordinate on the grid cell the prediction is coming from
                        * y_shifts: shape [1 x (num_anchors * num_cells) x 1],
                          y coordinate on the grid cell the prediction is coming from
                        * expanded_strides: shape [1 x (num_anchors * num_cells) x 1],
                          stride of the output grid the prediction is coming from
                        * transformed_outputs: shape [batch_size x (num_anchors * num_cells) x (num_classes + 5)],
                          predictions with boxes in real coordinates
                        * raw_outputs: shape [batch_size x (num_anchors * num_cells) x (num_classes + 5)],
                          raw predictions with boxes as logits

        """
        raw_outputs = []
        transformed_outputs = []
        x_shifts = []
        y_shifts = []
        expanded_strides = []
        for k, output in enumerate(predictions):
            batch_size, num_anchors, h, w, num_outputs = output.shape

            if self.grids[k].shape[2:4] != output.shape[2:4]:
                self.grids[k] = self._make_grid(w, h).type_as(output)

            # e.g. [batch_size, 1, 28, 28, 85] -> [batch_size, 784, 85]
            output_raveled = output.reshape(batch_size, num_anchors * h * w, num_outputs)
            # e.g [1, 784, 2]
            grid_raveled = self.grids[k].view(1, num_anchors * h * w, 2)
            if self.use_l1:
                # e.g [1, 784, 4]
                raw_outputs.append(output_raveled[:, :, :4].clone())

            output_raveled[..., :2] = (output_raveled[..., :2] + grid_raveled) * self.strides[k]
            output_raveled[..., 2:4] = torch.exp(output_raveled[..., 2:4]) * self.strides[k]

            # outputs with boxes in real coordinates, probs as logits
            transformed_outputs.append(output_raveled)
            # x cell coordinates of all 784 predictions, 0, 0, 0, ..., 1, 1, 1, ...
            x_shifts.append(grid_raveled[:, :, 0])
            # y cell coordinates of all 784 predictions, 0, 1, 2, ..., 0, 1, 2, ...
            y_shifts.append(grid_raveled[:, :, 1])
            # e.g. [1, 784], stride of this level (one of [8, 16, 32])
            expanded_strides.append(torch.zeros(1, grid_raveled.shape[1]).fill_(self.strides[k]).type_as(output))

        # all 4 below have shapes of [batch_size or 1, num_anchors * num_cells, num_values_pre_cell]
        # where num_anchors * num_cells is e.g. 1 * (28 * 28 + 14 * 14 + 17 * 17)
        transformed_outputs = torch.cat(transformed_outputs, 1)
        x_shifts = torch.cat(x_shifts, 1)
        y_shifts = torch.cat(y_shifts, 1)
        expanded_strides = torch.cat(expanded_strides, 1)
        if self.use_l1:
            raw_outputs = torch.cat(raw_outputs, 1)

        return x_shifts, y_shifts, expanded_strides, transformed_outputs, raw_outputs

    def get_l1_target(self, l1_target, gt, stride, x_shifts, y_shifts, eps=1e-8):
        l1_target[:, 0] = gt[:, 0] / stride - x_shifts
        l1_target[:, 1] = gt[:, 1] / stride - y_shifts
        l1_target[:, 2] = torch.log(gt[:, 2] / stride + eps)
        l1_target[:, 3] = torch.log(gt[:, 3] / stride + eps)
        return l1_target

    @torch.no_grad()
    def get_assignments(self, batch_idx, num_gt, total_num_anchors, gt_bboxes_per_image, gt_classes,
                        bboxes_preds_per_image, expanded_strides, x_shifts, y_shifts, cls_preds,
                        obj_preds, mode="gpu"):
        if mode == "cpu":
            print("------------CPU Mode for This Batch-------------")
            gt_bboxes_per_image = gt_bboxes_per_image.cpu().float()
            bboxes_preds_per_image = bboxes_preds_per_image.cpu().float()
            gt_classes = gt_classes.cpu().float()
            expanded_strides = expanded_strides.cpu().float()
            x_shifts = x_shifts.cpu()
            y_shifts = y_shifts.cpu()

        fg_mask, is_in_boxes_and_center = self.get_in_boxes_info(gt_bboxes_per_image, expanded_strides,
                                                                 x_shifts, y_shifts, total_num_anchors, num_gt)

        bboxes_preds_per_image = bboxes_preds_per_image[fg_mask]
        cls_preds_ = cls_preds[batch_idx][fg_mask]
        obj_preds_ = obj_preds[batch_idx][fg_mask]
        num_in_boxes_anchor = bboxes_preds_per_image.shape[0]

        if mode == "cpu":
            gt_bboxes_per_image = gt_bboxes_per_image.cpu()
            bboxes_preds_per_image = bboxes_preds_per_image.cpu()

        pair_wise_ious = bboxes_iou(gt_bboxes_per_image, bboxes_preds_per_image, False)

        gt_cls_per_image = F.one_hot(gt_classes.to(torch.int64), self.num_classes).float().unsqueeze(1).repeat(1, num_in_boxes_anchor, 1)
        pair_wise_ious_loss = -torch.log(pair_wise_ious + 1e-8)

        if mode == "cpu":
            cls_preds_, obj_preds_ = cls_preds_.cpu(), obj_preds_.cpu()

        with torch.cuda.amp.autocast(enabled=False):
            cls_preds_ = cls_preds_.float().unsqueeze(0).repeat(num_gt, 1, 1).sigmoid_() * \
                         obj_preds_.float().unsqueeze(0).repeat(num_gt, 1, 1).sigmoid_()
            pair_wise_cls_loss = F.binary_cross_entropy(cls_preds_.sqrt_(), gt_cls_per_image, reduction="none").sum(-1)
        del cls_preds_

        cost = pair_wise_cls_loss + 3.0 * pair_wise_ious_loss + 100000.0 * (~is_in_boxes_and_center)

        num_fg, gt_matched_classes, pred_ious_this_matching, matched_gt_inds = \
            self.dynamic_k_matching(cost, pair_wise_ious, gt_classes, num_gt, fg_mask)
        del pair_wise_cls_loss, cost, pair_wise_ious, pair_wise_ious_loss

        if mode == "cpu":
            gt_matched_classes = gt_matched_classes.cuda()
            fg_mask = fg_mask.cuda()
            pred_ious_this_matching = pred_ious_this_matching.cuda()
            matched_gt_inds = matched_gt_inds.cuda()

        return gt_matched_classes, fg_mask, pred_ious_this_matching, matched_gt_inds, num_fg

    def get_in_boxes_info(self, gt_bboxes_per_image, expanded_strides, x_shifts, y_shifts, total_num_anchors, num_gt):
        expanded_strides_per_image = expanded_strides[0]

        # cell coordinates, shape [n_predictions] -> repeated to [n_gts, n_predictions]
        x_shifts_per_image = x_shifts[0] * expanded_strides_per_image
        y_shifts_per_image = y_shifts[0] * expanded_strides_per_image
        x_centers_per_image = (x_shifts_per_image + 0.5 * expanded_strides_per_image).unsqueeze(0).repeat(num_gt, 1)
        y_centers_per_image = (y_shifts_per_image + 0.5 * expanded_strides_per_image).unsqueeze(0).repeat(num_gt, 1)

        # FIND CELL CENTERS THAT ARE WITHIN GROUND TRUTH BOXES

        # ground truth boxes, shape [n_gts] -> repeated to [n_gts, n_predictions]
        # from (c1, c2, w, h) to left, right, top, bottom
        gt_bboxes_per_image_l = (gt_bboxes_per_image[:, 0] -
                                 0.5 * gt_bboxes_per_image[:, 2]).unsqueeze(1).repeat(1, total_num_anchors)
        gt_bboxes_per_image_r = (gt_bboxes_per_image[:, 0] +
                                 0.5 * gt_bboxes_per_image[:, 2]).unsqueeze(1).repeat(1, total_num_anchors)
        gt_bboxes_per_image_t = (gt_bboxes_per_image[:, 1] -
                                 0.5 * gt_bboxes_per_image[:, 3]).unsqueeze(1).repeat(1, total_num_anchors)
        gt_bboxes_per_image_b = (gt_bboxes_per_image[:, 1] +
                                 0.5 * gt_bboxes_per_image[:, 3]).unsqueeze(1).repeat(1, total_num_anchors)

        # check which cell centers lay within the ground truth boxes
        b_l = x_centers_per_image - gt_bboxes_per_image_l  # x - l > 0 when l is on the lest from x
        b_r = gt_bboxes_per_image_r - x_centers_per_image
        b_t = y_centers_per_image - gt_bboxes_per_image_t
        b_b = gt_bboxes_per_image_b - y_centers_per_image
        bbox_deltas = torch.stack([b_l, b_t, b_r, b_b], 2)  # shape [n_gts, n_predictions]

        # to claim that a cell center is inside a gt box all 4 differences calculated above should be positive
        is_in_boxes = bbox_deltas.min(dim=-1).values > 0.0  # shape [n_gts, n_predictions]
        is_in_boxes_all = is_in_boxes.sum(dim=0) > 0  # shape [n_predictions], whether a cell is inside at least one gt

        # FIND CELL CENTERS THAT ARE WITHIN +- self.center_sampling_radius CELLS FROM GROUND TRUTH BOXES CENTERS

        # define fake boxes: instead of ground truth boxes step +- self.center_sampling_radius from their centers
        gt_bboxes_per_image_l = (gt_bboxes_per_image[:, 0]).unsqueeze(1).repeat(1, total_num_anchors) - \
                                self.center_sampling_radius * expanded_strides_per_image.unsqueeze(0)
        gt_bboxes_per_image_r = (gt_bboxes_per_image[:, 0]).unsqueeze(1).repeat(1, total_num_anchors) + \
                                self.center_sampling_radius * expanded_strides_per_image.unsqueeze(0)
        gt_bboxes_per_image_t = (gt_bboxes_per_image[:, 1]).unsqueeze(1).repeat(1, total_num_anchors) - \
                                self.center_sampling_radius * expanded_strides_per_image.unsqueeze(0)
        gt_bboxes_per_image_b = (gt_bboxes_per_image[:, 1]).unsqueeze(1).repeat(1, total_num_anchors) + \
                                self.center_sampling_radius * expanded_strides_per_image.unsqueeze(0)

        c_l = x_centers_per_image - gt_bboxes_per_image_l
        c_r = gt_bboxes_per_image_r - x_centers_per_image
        c_t = y_centers_per_image - gt_bboxes_per_image_t
        c_b = gt_bboxes_per_image_b - y_centers_per_image
        center_deltas = torch.stack([c_l, c_t, c_r, c_b], 2)
        is_in_centers = center_deltas.min(dim=-1).values > 0.0
        is_in_centers_all = is_in_centers.sum(dim=0) > 0

        # in boxes and in centers
        is_in_boxes_anchor = is_in_boxes_all | is_in_centers_all

        is_in_boxes_and_center = (is_in_boxes[:, is_in_boxes_anchor] & is_in_centers[:, is_in_boxes_anchor])
        return is_in_boxes_anchor, is_in_boxes_and_center

    def dynamic_k_matching(self, cost, pair_wise_ious, gt_classes, num_gt, fg_mask):
        matching_matrix = torch.zeros_like(cost, dtype=torch.uint8)

        ious_in_boxes_matrix = pair_wise_ious
        n_candidate_k = min(10, ious_in_boxes_matrix.size(1))
        topk_ious, _ = torch.topk(ious_in_boxes_matrix, n_candidate_k, dim=1)
        dynamic_ks = torch.clamp(topk_ious.sum(1).int(), min=1)
        dynamic_ks = dynamic_ks.tolist()
        for gt_idx in range(num_gt):
            _, pos_idx = torch.topk(cost[gt_idx], k=dynamic_ks[gt_idx], largest=False)
            matching_matrix[gt_idx][pos_idx] = 1

        del topk_ious, dynamic_ks, pos_idx

        anchor_matching_gt = matching_matrix.sum(0)
        if (anchor_matching_gt > 1).sum() > 0:
            _, cost_argmin = torch.min(cost[:, anchor_matching_gt > 1], dim=0)
            matching_matrix[:, anchor_matching_gt > 1] *= 0
            matching_matrix[cost_argmin, anchor_matching_gt > 1] = 1
        fg_mask_inboxes = matching_matrix.sum(0) > 0
        num_fg = fg_mask_inboxes.sum().item()

        fg_mask[fg_mask.clone()] = fg_mask_inboxes

        matched_gt_inds = matching_matrix[:, fg_mask_inboxes].argmax(0)
        gt_matched_classes = gt_classes[matched_gt_inds]

        pred_ious_this_matching = (matching_matrix * pair_wise_ious).sum(0)[fg_mask_inboxes]
        return num_fg, gt_matched_classes, pred_ious_this_matching, matched_gt_inds