import os
import cv2
import numpy as np
from typing import Union, Callable
import torch
import torch.nn.functional as F
from torchvision.utils import draw_segmentation_masks

# FIXME: REFACTOR AUGMENTATIONS, CONSIDER USING A MORE EFFICIENT LIBRARIES SUCH AS, IMGAUG, DALI ETC.
from super_gradients.training import utils as core_utils


def coco_sub_classes_inclusion_tuples_list():
    return [(0, 'background'), (5, 'airplane'), (2, 'bicycle'), (16, 'bird'),
            (9, 'boat'),
            (44, 'bottle'), (6, 'bus'), (3, 'car'), (17, 'cat'), (62, 'chair'),
            (21, 'cow'),
            (67, 'dining table'), (18, 'dog'), (19, 'horse'), (4, 'motorcycle'),
            (1, 'person'),
            (64, 'potted plant'), (20, 'sheep'), (63, 'couch'), (7, 'train'),
            (72, 'tv')]


def to_one_hot(target: torch.Tensor, num_classes: int, ignore_index: int = None):
    """
    Target label to one_hot tensor. labels and ignore_index must be consecutive numbers.
    :param target: Class labels long tensor, with shape [N, H, W]
    :param num_classes: num of classes in datasets excluding ignore label, this is the output channels of the one hot
        result.
    :return: one hot tensor with shape [N, num_classes, H, W]
    """
    num_classes = num_classes if ignore_index is None else num_classes + 1

    one_hot = F.one_hot(target, num_classes).permute((0, 3, 1, 2))

    if ignore_index is not None:
        # remove ignore_index channel
        one_hot = torch.cat([one_hot[:, :ignore_index], one_hot[:, ignore_index + 1:]], dim=1)

    return one_hot


def reverse_imagenet_preprocessing(im_tensor: torch.Tensor) -> np.ndarray:
    """
    :param im_tensor: images in a batch after preprocessing for inference, RGB, (B, C, H, W)
    :return:          images in a batch in cv2 format, BGR, (B, H, W, C)
    """
    im_np = im_tensor.cpu().numpy()
    im_np = im_np[:, ::-1, :, :].transpose(0, 2, 3, 1)
    im_np *= np.array([[[.229, .224, .225][::-1]]])
    im_np += np.array([[[.485, .456, .406][::-1]]])
    im_np *= 255.
    return np.ascontiguousarray(im_np, dtype=np.uint8)


class BinarySegmentationVisualization:

    @staticmethod
    def _visualize_image(image_np: np.ndarray, pred_mask: torch.Tensor, target_mask: torch.Tensor,
                         image_scale: float, checkpoint_dir: str, image_name: str):
        pred_mask = pred_mask.copy()
        image_np = torch.from_numpy(np.moveaxis(image_np, -1, 0).astype(np.uint8))

        pred_mask = pred_mask[np.newaxis, :, :] > 0.5
        target_mask = target_mask[np.newaxis, :, :].astype(bool)
        tp_mask = np.logical_and(pred_mask, target_mask)
        fp_mask = np.logical_and(pred_mask, np.logical_not(target_mask))
        fn_mask = np.logical_and(np.logical_not(pred_mask), target_mask)
        overlay = torch.from_numpy(np.concatenate([tp_mask, fp_mask, fn_mask]))

        # SWITCH BETWEEN BLUE AND RED IF WE SAVE THE IMAGE ON THE DISC AS OTHERWISE WE CHANGE CHANNEL ORDERING
        colors = ['green', 'red', 'blue']
        res_image = draw_segmentation_masks(image_np, overlay, colors=colors).detach().numpy()
        res_image = np.concatenate([res_image[ch, :, :, np.newaxis] for ch in range(3)], 2)
        res_image = cv2.resize(res_image.astype(np.uint8), (0, 0), fx=image_scale, fy=image_scale,
                               interpolation=cv2.INTER_NEAREST)

        if checkpoint_dir is None:
            return res_image
        else:
            cv2.imwrite(os.path.join(checkpoint_dir, str(image_name) + '.jpg'), res_image)

    @staticmethod
    def visualize_batch(image_tensor: torch.Tensor, pred_mask: torch.Tensor, target_mask: torch.Tensor,
                        batch_name: Union[int, str], checkpoint_dir: str = None,
                        undo_preprocessing_func: Callable[[torch.Tensor], np.ndarray] = reverse_imagenet_preprocessing,
                        image_scale: float = 1.):
        """
        A helper function to visualize detections predicted by a network:
        saves images into a given path with a name that is {batch_name}_{imade_idx_in_the_batch}.jpg, one batch per call.
        Colors are generated on the fly: uniformly sampled from color wheel to support all given classes.

        :param image_tensor:            rgb images, (B, H, W, 3)
        :param pred_boxes:              boxes after NMS for each image in a batch, each (Num_boxes, 6),
                                        values on dim 1 are: x1, y1, x2, y2, confidence, class
        :param target_boxes:            (Num_targets, 6), values on dim 1 are: image id in a batch, class, x y w h
                                        (coordinates scaled to [0, 1])
        :param batch_name:              id of the current batch to use for image naming

        :param checkpoint_dir:          a path where images with boxes will be saved. if None, the result images will
                                        be returns as a list of numpy image arrays

        :param undo_preprocessing_func: a function to convert preprocessed images tensor into a batch of cv2-like images
        :param image_scale:             scale factor for output image
        """
        image_np = undo_preprocessing_func(image_tensor.detach())
        pred_mask = torch.sigmoid(pred_mask[:, 0, :, :])  # comment out

        out_images = []
        for i in range(image_np.shape[0]):
            preds = pred_mask[i].detach().cpu().numpy()
            targets = target_mask[i].detach().cpu().numpy()

            image_name = '_'.join([str(batch_name), str(i)])
            res_image = BinarySegmentationVisualization._visualize_image(image_np[i], preds, targets, image_scale,
                                                                         checkpoint_dir, image_name)
            if res_image is not None:
                out_images.append(res_image)

        return out_images


def visualize_batches(dataloader, module, visualization_path, num_batches=1, undo_preprocessing_func=None):
    os.makedirs(visualization_path, exist_ok=True)
    for batch_i, (imgs, targets) in enumerate(dataloader):
        if batch_i == num_batches:
            return
        imgs = core_utils.tensor_container_to_device(imgs, torch.device('cuda:0'))
        targets = core_utils.tensor_container_to_device(targets, torch.device('cuda:0'))
        pred_mask = module(imgs)

        # Visualize the batch
        if undo_preprocessing_func:
            BinarySegmentationVisualization.visualize_batch(imgs, pred_mask, targets, batch_i, visualization_path,
                                                            undo_preprocessing_func=undo_preprocessing_func)
        else:
            BinarySegmentationVisualization.visualize_batch(imgs, pred_mask, targets, batch_i, visualization_path)
