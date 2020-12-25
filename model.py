"""A module for constructing machine learning models."""
import torch.nn as nn


import sys

sys.path.insert(0, "./gen-efficientnet-pytorch-master-hongnan")
import geffnet


class CustomEfficientNet(nn.Module):

    """A custom EfficientNet model."""

    def __init__(self, config: type, pretrained: bool = True):
        """Construct a custom EfficientNet model."""
        super().__init__()
        self.config = config
        self.model = geffnet.create_model(
            model_weight_path_folder=config.paths["model_weight_path_folder"],
            model_name=config.effnet,
            pretrained=pretrained,
        )
        n_features = self.model.classifier.in_features
        self.model.classifier = nn.Linear(n_features, config.num_classes)

    # TODO: Change x to input_neurons, I feel it is more apt.
    def forward(self, input_neurons):
        """Define the computation performed at every call."""
        # TODO: add dropout layers, or the likes.
        output_predictions = self.model(input_neurons)
        return output_predictions