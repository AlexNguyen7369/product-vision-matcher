from transformers import ViTImageProcessor, ViTModel
import torch
from PIL import Image

# Create instances of preprocessor and model

# downloads processor confuguration from HuggingFace, tells processor how to preprocess the image, e.g. resizing, normalization
processor = ViTImageProcessor.from_pretrained('google/vit-base-patch16-224')

# model pretrained with million of images, millions of learned parameters
model = ViTModel.from_pretrained('google/vit-base-patch16-224')


model = model.eval()  # Set the model to evaluation mode
# Only using the model, not training, also makes output deterministic

def generate_embeddings(image_path: str) -> torch.Tensor:
    # load and preprocess the image
    image = Image.open(image_path).convert('RGB')  # Open the image and convert to RGB
    inputs = processor(images=image, return_tensors='pt')  # pt is for PyTorch, returns a dictionary with 'pixel_values' key
    with torch.no_grad():  # Disable gradient calculation
        outputs = model(**inputs)  # Pass the preprocessed image through the model
    embeddings = outputs.last_hidden_state[:, 0, :].squeeze()  # Extract the [CLS] token embedding
    return embeddings

if __name__ == '__main__':
    import os

    image_dir = 'data/catalog/images'
    for filename in os.listdir(image_dir):
        if filename.endswith('.jpg'):
            path = os.path.join(image_dir, filename)
            embedding = generate_embeddings(path)
            print(f"{filename}: shape={embedding.shape}, dtype={embedding.dtype}")