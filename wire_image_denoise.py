#!/usr/bin/env python

import os
from tqdm import tqdm
import time
from dotenv import load_dotenv

load_dotenv()

import numpy as np
from scipy import io
import wandb
import argparse

import matplotlib.pyplot as plt

plt.gray()

import cv2
from skimage.metrics import structural_similarity as ssim_func

import torch
import torch.nn
from torch.optim.lr_scheduler import LambdaLR
from pytorch_msssim import ssim


from modules import models
from modules import utils

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Image reconstruction parameters")
    parser.add_argument(
        "-n",
        "--nonlinearity",
        choices=["wire", "siren", "mfn", "relu", "posenc", "gauss"],
        type=str,
        help="Name of nonlinearity",
        default="wire",
    )
    parser.add_argument(
        "-i",
        "--input_image",
        type=str,
        help="Input image name",
        default="parrot.png",
    )
    args = parser.parse_args()
    nonlin = args.nonlinearity
    img_name_ext = args.input_image

    niters = 2000  # Number of SGD iterations
    learning_rate = 5e-3  # Learning rate.

    # WIRE works best at 5e-3 to 2e-2, Gauss and SIREN at 1e-3 - 2e-3,
    # MFN at 1e-2 - 5e-2, and positional encoding at 5e-4 to 1e-3

    tau = 3e1  # Photon noise (max. mean lambda). Set to 3e7 for representation, 3e1 for denoising
    noise_snr = 2  # Readout noise (dB)

    # Gabor filter constants.
    # We suggest omega0 = 4 and sigma0 = 4 for denoising, and omega0=20, sigma0=30 for image representation
    omega0 = 5.0  # Frequency of sinusoid
    sigma0 = 5.0  # Sigma of Gaussian

    # Network parameters
    hidden_layers = 2  # Number of hidden layers in the MLP
    hidden_features = 256  # Number of hidden units per layer
    maxpoints = 256 * 256  # Batch size

    # Read image and scale. A scale of 0.5 for parrot image ensures that it
    # fits in a 12GB GPU
    img_name = img_name_ext.split(".")[0]
    img_path = os.path.join("data", img_name_ext)

    im = utils.normalize(plt.imread(img_path).astype(np.float32), True)
    # im = cv2.resize(im, None, fx=1 / 2, fy=1 / 2, interpolation=cv2.INTER_AREA)

    if len(im.shape) == 2:
      H, W = im.shape
      D = 1
    else:
      H, W, D = im.shape

    if os.getenv("WANDB_LOG") in ["true", "True", True]:
        run_name = (
            f'{nonlin}_{img_name}_image_denoise__{str(time.time()).replace(".", "_")}'
        )
        xp = wandb.init(
            name=run_name, project="pracnet", resume="allow", anonymous="allow"
        )

    # Create a noisy image
    im_noisy = utils.measure(im, noise_snr, tau)

    if nonlin == "posenc":
        nonlin = "relu"
        posencode = True

        if tau < 100:
            sidelength = int(max(H, W) / 3)
        else:
            sidelength = int(max(H, W))

    else:
        posencode = False
        sidelength = H

    model = models.get_INR(
        nonlin=nonlin,
        in_features=2,
        out_features=D,
        hidden_features=hidden_features,
        hidden_layers=hidden_layers,
        first_omega_0=omega0,
        hidden_omega_0=omega0,
        scale=sigma0,
        pos_encode=posencode,
        sidelength=sidelength,
    )

    # Send model to CUDA
    model.cuda()

    print("Number of parameters: ", utils.count_parameters(model))
    print("Input PSNR: %.2f dB" % utils.psnr(im, im_noisy))

    # Create an optimizer
    optim = torch.optim.Adam(
        lr=learning_rate * min(1, maxpoints / (H * W)), params=model.parameters()
    )

    # Schedule to reduce lr to 0.1 times the initial rate in final epoch
    scheduler = LambdaLR(optim, lambda x: 0.1 ** min(x / niters, 1))

    x = torch.linspace(-1, 1, W)
    y = torch.linspace(-1, 1, H)

    X, Y = torch.meshgrid(x, y, indexing="xy")
    coords = torch.hstack((X.reshape(-1, 1), Y.reshape(-1, 1)))[None, ...]

    gt = torch.tensor(im).cuda().reshape(H * W, D)[None, ...]
    gt_noisy = torch.tensor(im_noisy).cuda().reshape(H * W, D)[None, ...]

    mse_array = torch.zeros(niters, device="cuda")
    mse_loss_array = torch.zeros(niters, device="cuda")
    time_array = torch.zeros_like(mse_array)

    best_mse = torch.tensor(float("inf"))
    best_img = None

    rec = torch.zeros_like(gt)

    tbar = tqdm(range(niters))
    init_time = time.time()
    for epoch in tbar:
        indices = torch.randperm(H * W)

        train_loss = cnt = 0
        for b_idx in range(0, H * W, maxpoints):
            b_indices = indices[b_idx : min(H * W, b_idx + maxpoints)]
            b_coords = coords[:, b_indices, ...].cuda()
            b_indices = b_indices.cuda()
            pixelvalues = model(b_coords)

            with torch.no_grad():
                rec[:, b_indices, :] = pixelvalues

            loss = ((pixelvalues - gt_noisy[:, b_indices, :]) ** 2).mean()
            train_loss += loss.item()

            optim.zero_grad()
            loss.backward()
            optim.step()

            cnt += 1

        time_array[epoch] = time.time() - init_time

        with torch.no_grad():
            mse_loss_array[epoch] = ((gt_noisy - rec) ** 2).mean().item()
            mse_array[epoch] = ((gt - rec) ** 2).mean().item()
            im_gt = gt.reshape(H, W, D).permute(2, 0, 1)[None, ...]
            im_rec = rec.reshape(H, W, D).permute(2, 0, 1)[None, ...]

            psnrval = -10 * torch.log10(mse_array[epoch])
            tbar.set_description("%.1f" % psnrval)
            tbar.refresh()

            if os.getenv("WANDB_LOG") in ["true", "True", True]:
                xp.log({"loss": train_loss / cnt, "psnr": psnrval})

        scheduler.step()

        imrec = rec[0, ...].reshape(H, W, D).detach().cpu().numpy()

        cv2.imshow("Reconstruction", imrec[..., ::-1])
        cv2.waitKey(1)

        if (mse_array[epoch] < best_mse) or (epoch == 0):
            best_mse = mse_array[epoch]
            best_img = imrec

    if posencode:
        nonlin = "posenc"

    mdict = {
        "rec": best_img,
        "gt": im,
        "im_noisy": im_noisy,
        "mse_noisy_array": mse_loss_array.detach().cpu().numpy(),
        "mse_array": mse_array.detach().cpu().numpy(),
        "time_array": time_array.detach().cpu().numpy(),
    }

    img_name = img_name_ext.split(".")[0]

    os.makedirs(
        os.path.join(os.getenv("RESULTS_SAVE_PATH"), "denoising"),
        exist_ok=True,
    )
    io.savemat(
        os.path.join(
            os.getenv("RESULTS_SAVE_PATH"),
            "denoising",
            f"{nonlin}_{img_name}.mat",
        ),
        mdict,
    )

    print("Best PSNR: %.2f dB" % utils.psnr(im, best_img))

    # save model
    os.makedirs(
        os.path.join(os.getenv("MODEL_SAVE_PATH"), "denoising"),
        exist_ok=True,
    )
    torch.save(
        model.state_dict(),
        os.path.join(
            os.getenv("MODEL_SAVE_PATH"),
            "denoising",
            f"{nonlin}_{img_name}.pth",
        ),
    )

    plt.imshow(best_img)
    plt.savefig(
        os.path.join(
            os.getenv("RESULTS_SAVE_PATH"), "denoising", f"{nonlin}_{img_name}.png"
        )
    )

    print("saving the image on WANDB")
    wandb.log(
        {
            f"{nonlin}_image_reconst": [
                wandb.Image(best_img, caption="Reconstructed image.")
            ]
        }
    )
