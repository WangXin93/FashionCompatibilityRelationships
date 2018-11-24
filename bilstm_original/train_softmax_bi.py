import argparse
import logging
import numpy as np
import os
import sys
import torch
import torch.nn as nn
import torch.nn.utils.rnn as rnn_utils
import torchvision
from model import EncoderCNN, LSTMModel
from polyvore_dataset import create_dataloader
from torch.optim import lr_scheduler
from torch.utils.data import DataLoader

################################# Argparse ####################################
parser = argparse.ArgumentParser(description="Polyvore BiLSTM")
parser.add_argument("--model", type=str, default="lstm")
parser.add_argument("--epochs", type=int, default=30)
parser.add_argument("--batch_size", type=int, default=16)
parser.add_argument("--comment", type=str, default="")
args = parser.parse_args()
print(args)

###############################################################################

epochs = args.epochs
batch_size = args.batch_size
comment = args.comment
model = args.model
emb_size = 512
log_step = 50
device = torch.device("cuda")

################################# DataLoader ##################################
img_size = 299

train_dataset, train_loader = create_dataloader(
    batch_size=batch_size,
    shuffle=True,
    num_workers=4,
    which_set="train",
    img_size=img_size,
)
_, val_loader = create_dataloader(
    batch_size=batch_size,
    shuffle=False,
    num_workers=4,
    which_set="valid",
    img_size=img_size,
)
_, test_loader = create_dataloader(
    batch_size=batch_size,
    shuffle=False,
    num_workers=4,
    which_set="test",
    img_size=img_size,
)

###############################################################################

encoder_cnn = EncoderCNN(emb_size)
encoder_cnn = encoder_cnn.to(device)

if model == "lstm":
    f_rnn = LSTMModel(emb_size, emb_size, emb_size, device, bidirectional=False)
    b_rnn = LSTMModel(emb_size, emb_size, emb_size, device, bidirectional=False)
f_rnn = f_rnn.to(device)
b_rnn = b_rnn.to(device)

criterion = nn.CrossEntropyLoss()
params_to_train = (
    list(encoder_cnn.parameters()) + list(f_rnn.parameters()) + list(b_rnn.parameters())
)
optimizer = torch.optim.SGD(params_to_train, lr=2e-1, momentum=0.9)
scheduler = lr_scheduler.StepLR(optimizer, step_size=2, gamma=0.5)

################################## Logger #####################################
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

file_handler = logging.FileHandler("log_{}{}.log".format(__file__.split(".")[0], comment))
log_format = "%(asctime)s [%(levelname)-5.5s] %(message)s"
formatter = logging.Formatter(log_format)
file_handler.setFormatter(formatter)
file_handler.setLevel(logging.INFO)

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)

logger.addHandler(stream_handler)
logger.addHandler(file_handler)

################################## Train ######################################
def flip_tensor(tensor, device=device):
    """Flip a tensor in 0 dim for backward rnn.
    """
    idx = [i for i in range(tensor.size(0) - 1, -1, -1)]
    idx = torch.LongTensor(idx).to(device)
    flipped_tensor = tensor.index_select(0, idx)
    return flipped_tensor


def train():
    for epoch in range(1, epochs + 1):
        # Train phase
        total_loss = 0
        scheduler.step()
        encoder_cnn.train(True)
        f_rnn.train(True)
        b_rnn.train(True)
        for batch_num, input_data in enumerate(train_loader, 1):
            lengths, names, likes, descs, images, image_ids = input_data
            image_seqs = images.to(device)  # (20+, 3, 224, 224)
            emb_seqs = encoder_cnn(image_seqs)  # (20+, 512)

            # Generate input embeddings e.g. (1, 2, 3, 4)
            input_emb_list = []
            start = 0
            for length in lengths:
                input_emb_list.append(emb_seqs[start : start + length - 1])
                start += length
            f_input_embs = rnn_utils.pad_sequence(
                input_emb_list, batch_first=True
            )  # (4, 7, 512) (1, 2, 3, 4)
            b_target_embs = rnn_utils.pad_sequence(
                [flip_tensor(e) for e in input_emb_list], batch_first=True
            )  # (4, 3, 2, 1)

            # Generate target embeddings e.g. (2, 3, 4, 5)
            target_emb_list = []
            start = 0
            for length in lengths:
                target_emb_list.append(emb_seqs[start + 1 : start + length])
                start += length
            f_target_embs = rnn_utils.pad_sequence(
                target_emb_list, batch_first=True
            )  # (2, 3, 4, 5)
            b_input_embs = rnn_utils.pad_sequence(
                [flip_tensor(e) for e in target_emb_list], batch_first=True
            )  # (5, 4, 3, 2)

            seq_lengths = torch.tensor([i - 1 for i in lengths]).to(device)
            f_target_embs = rnn_utils.pack_padded_sequence(
                f_target_embs, seq_lengths, batch_first=True
            )[0]
            b_target_embs = rnn_utils.pack_padded_sequence(
                b_target_embs, seq_lengths, batch_first=True
            )[0]

            f_output = f_rnn(f_input_embs, seq_lengths)
            f_score = torch.matmul(f_output, f_target_embs.t())
            f_loss = criterion(f_score, torch.arange(f_score.shape[0]).to(device))
            b_output = b_rnn(b_input_embs, seq_lengths)
            b_score = torch.matmul(b_output, b_target_embs.t())
            b_loss = criterion(b_score, torch.arange(b_score.shape[0]).to(device))
            all_loss = f_loss + b_loss

            encoder_cnn.zero_grad()
            f_rnn.zero_grad()
            b_rnn.zero_grad()
            all_loss.backward()
            nn.utils.clip_grad_norm_(params_to_train, 0.5)  # clip gradient
            optimizer.step()

            total_loss += all_loss.item()
            # Print log info
            if batch_num % log_step == 0:
                logger.info(
                    "Epoch [{}/{}], Step #{}, F_loss: {:.4f}, B_loss: {:.4f}, All_loss: {:.4f}".format(
                        epoch,
                        epochs,
                        batch_num,
                        f_loss.item(),
                        b_loss.item(),
                        all_loss.item(),
                    )
                )

        logger.info(
            "**Epoch {}**, Train Loss {:.4f}".format(epoch, total_loss / batch_num)
        )
        # Save the model checkpoints
        torch.save(
            f_rnn.state_dict(), os.path.join("f_rnn{}.pth".format(comment))
        )
        torch.save(
            b_rnn.state_dict(), os.path.join("b_rnn{}.pth".format(comment))
        )
        torch.save(
            encoder_cnn.state_dict(),
            os.path.join("encoder_cnn{}.pth".format(comment)),
        )

        # Validate phase !!!
        encoder_cnn.train(False)  # eval mode (batchnorm uses moving mean/variance
        f_rnn.train(False)  # eval mode (batchnorm uses moving mean/variance
        b_rnn.train(False)  # eval mode (batchnorm uses moving mean/variance
        total_loss = 0
        for batch_num, input_data in enumerate(val_loader, 1):
            lengths, names, likes, descs, images, image_ids = input_data
            image_seqs = images.to(device)  # (20+, 3, 224, 224)
            with torch.no_grad():
                emb_seqs = encoder_cnn(image_seqs)  # (20+, 512)

            # Generate input embeddings e.g. (1, 2, 3, 4)
            input_emb_list = []
            start = 0
            for length in lengths:
                input_emb_list.append(emb_seqs[start : start + length - 1])
                start += length
            f_input_embs = rnn_utils.pad_sequence(
                input_emb_list, batch_first=True
            )  # (4, 7, 512) (1, 2, 3, 4)
            b_target_embs = rnn_utils.pad_sequence(
                [flip_tensor(e) for e in input_emb_list], batch_first=True
            )  # (4, 3, 2, 1)

            # Generate target embeddings e.g. (2, 3, 4, 5)
            target_emb_list = []
            start = 0
            for length in lengths:
                target_emb_list.append(emb_seqs[start + 1 : start + length])
                start += length
            f_target_embs = rnn_utils.pad_sequence(
                target_emb_list, batch_first=True
            )  # (2, 3, 4, 5)
            b_input_embs = rnn_utils.pad_sequence(
                [flip_tensor(e) for e in target_emb_list], batch_first=True
            )  # (5, 4, 3, 2)

            seq_lengths = torch.tensor([i - 1 for i in lengths]).to(device)
            f_target_embs = rnn_utils.pack_padded_sequence(
                f_target_embs, seq_lengths, batch_first=True
            )[0]
            b_target_embs = rnn_utils.pack_padded_sequence(
                b_target_embs, seq_lengths, batch_first=True
            )[0]

            with torch.no_grad():
                f_output = f_rnn(f_input_embs, seq_lengths)
                f_score = torch.matmul(f_output, f_target_embs.t())
                f_loss = criterion(f_score, torch.arange(f_score.shape[0]).to(device))
                b_output = b_rnn(b_input_embs, seq_lengths)
                b_score = torch.matmul(b_output, b_target_embs.t())
                b_loss = criterion(b_score, torch.arange(b_score.shape[0]).to(device))
                all_loss = f_loss + b_loss

            total_loss += all_loss.item()

        logger.info(
            "**Epoch {}**, Valid Loss {:.4f}".format(epoch, total_loss / batch_num)
        )


if __name__ == "__main__":
    train()
