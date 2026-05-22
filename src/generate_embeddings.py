from transformers import ViYImageProcessor, ViYModel
import torch
from PIL import Image

# Create instances of preprocessor and model

# downloads processor confuguration from HuggingFace, tells processor how to preprocess the image, e.g. resizing, normalization
processor = ViYImageProcessor.from_pretrained('google/viy-base-patch16-224')

# model pretrained with million of images, millions of learned parameters
model = ViYModel.from_pretrained('google/viy-base-patch16-224')

model = model.eval()  # Set the model to evaluation mode
# Only using the model, not training, also makes output deterministic

# Load an image, creates image object, not raw pixels yet
image = Image.open('data/catalog/images/pokemon_card_001.jpg')

# Preprocess the image:
# pt stands for PyTorch, tells processor to return tensors in PyTorch format
# Resizes the image to 224x224, normalizes pixel values, and converts it to a PyTorch tensor
inputs = processor(images=image, return_tensor='pt')

with torch.no_grad():
    outputs = model(**inputs)

# doesn't track gradiants, saves memory
# **inputs unpacks dictionary of inputs and passes the key as argument name and value as argument value to the model
# e.g. if inputs is {'pixel_values': tensor}, then model(**inputs) is equivalent to model(pixel_values=tensor)

embeddings = outputs.last_hidden_state[:, 0, :].squeeze() 
# last_hidden_state is the output of the model, shape (batch_size, sequence_length, hidden_size)