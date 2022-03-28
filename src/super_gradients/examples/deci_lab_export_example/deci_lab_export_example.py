"""
Deci-lab model export example.

The main purpose of this code is to demonstrate how to upload the model to the platform, optimize and download it
 after training is complete, using DeciPlatformCallback.
"""
from super_gradients import SgModel, ClassificationTestDatasetInterface
from super_gradients.training.metrics import Accuracy, Top5
from super_gradients.training.models import ResNet18
from torch.optim import SGD
from super_gradients.training.utils.callbacks import DeciLabUploadCallback, ModelConversionCheckCallback
from deci_lab_client.models import Metric, QuantizationLevel, ModelMetadata, OptimizationRequestForm, HardwareType, \
    FrameworkType

# Empty on purpose so that it can be fit to the trainer use case
checkpoint_dir = ''
auth_token='eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiJ9.eyJzdWIiOiJ0ZXN0Y2FzZUBkZWNpLmFpIiwiY29tcGFueV9pZCI6IjAzZDdmZTJmLTA4MTgtNGZkOC04M2FiLWVjNWVlMGU5MDAyOCIsImNvbXBhbnlfbmFtZSI6InRlc3QiLCJ1c2VyX2lkIjoiMjhjODA3ZTYtNzA5MS00ODgyLTg0ZGItNjY4OGI5MWE3MjA0Iiwic291cmNlIjoiUGxhdGZvcm0iLCJleHAiOjkxMTM0MzM4MjV9.h4-J3oW2DGqSMS3O9S3gE8Up6dQ2DMOHkIhXZpuQeRX1wTJxXUQf-4OJ8-QuFDiQyS2u_XLb6JQ9GuOi-MZG6Q'
model = SgModel("lab_optimization_resnet18_example", model_checkpoints_location='local', ckpt_root_dir=checkpoint_dir)
dataset = ClassificationTestDatasetInterface(dataset_params={"batch_size": 10})
model.connect_dataset_interface(dataset)

net = ResNet18(num_classes=5, arch_params={})
optimizer = SGD(params=net.parameters(), lr=0.1)
model.build_model(net)

# CREATE META-DATA, AND OPTIMIZATION REQUEST FORM FOR DECI PLATFORM POST TRAINING CALLBACK
model_meta_data = ModelMetadata(
    name='resnet18_for_deci_lab_export_example',
    primary_batch_size=1,
    architecture='Resnet18',
    framework=FrameworkType.PYTORCH,
    dl_task='classification',
    input_dimensions=(3, 224, 224),
    primary_hardware=HardwareType.K80,
    dataset_name='imagenet',
    description='ResNet18 ONNX deci.ai Test',
    tags=['imagenet', 'resnet18'],
)

optimization_request_form = OptimizationRequestForm(target_hardware=HardwareType.T4,
                                                    target_batch_size=1,
                                                    target_metric=Metric.LATENCY,
                                                    optimize_model_size=True,
                                                    quantization_level=QuantizationLevel.FP16,
                                                    optimize_autonac=True)

# IT IS ALSO RECOMMENDED TO USE A PRE TRAINING MODEL CONVERSION CHECK CALLBACK, SO THAT ANY CONVERSION
# ERRORS WON'T APPEAR FOR THE FIRST TIME ONLY AT THE END OF TRAINING:

phase_callbacks = [
    ModelConversionCheckCallback(model_meta_data=model_meta_data),
    DeciLabUploadCallback(
        model_meta_data=model_meta_data,
        optimization_request_form=optimization_request_form,
        auth_token=auth_token
    )
]

# DEFINE TRAINING PARAMETERS
train_params = {"max_epochs": 2,
                "lr_updates": [1],
                "lr_decay_factor": 0.1,
                "lr_mode": "step",
                "lr_warmup_epochs": 0,
                "initial_lr": 0.1,
                "loss": "cross_entropy",
                "optimizer": optimizer,
                "criterion_params": {},
                "train_metrics_list": [Accuracy(), Top5()],
                "valid_metrics_list": [Accuracy(), Top5()],
                "loss_logging_items_names": ["Loss"],
                "metric_to_watch": "Accuracy",
                "greater_metric_to_watch_is_better": True,
                "phase_callbacks": phase_callbacks}

# RUN TRAINING. ONCE ALL EPOCHS ARE DONE THE OPTIMIZED MODEL FILE WILL BE LOCATED IN THE EXPERIMENT'S
# CHECKPOINT DIRECTORY
if __name__ == '__main__':
    model.train(train_params)
