#!/usr/bin/env python

import os
import sys
import tqdm
import time
import copy
import wandb
from dotenv import load_dotenv
load_dotenv()

import numpy as np
from scipy import io
from scipy import ndimage
import cv2
import tripy
import numpy as np
from plyfile import PlyData, PlyElement
import scipy.io

import torch
from torch.optim.lr_scheduler import LambdaLR

import matplotlib.pyplot as plt

plt.gray()

from modules import models
from modules import utils
from modules import volutils

import argparse

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Occupancy parameters")
    parser.add_argument(
        "-n",
        "--nonlinearity",
        choices=["wire", "siren", "mfn", "relu", "posenc", "gauss"],
        type=str,
        help="Name of nonlinearity",
        default="wire",
    )
    args = parser.parse_args()
    nonlin = args.nonlinearity

    niters = 200  # Number of SGD iterations
    learning_rate = 5e-3  # Learning rate
    expname = "thai_statue"  # Volume to load
    scale = 1.0  # Run at lower scales to testing
    mcubes_thres = 0.5  # Threshold for marching cubes

    # Gabor filter constants
    # These settings work best for 3D occupancies
    omega0 = 10.0  # Frequency of sinusoid
    sigma0 = 40.0  # Sigma of Gaussian

    # Network constants
    hidden_layers = 2  # Number of hidden layers in the mlp
    hidden_features = 256  # Number of hidden units per layer
    maxpoints = int(2e5)  # Batch size

    if os.getenv("WANDB_LOG") in ["true", "True", True]:
        run_name = f'{nonlin}_{expname}_occupancy__{str(time.time()).replace(".", "_")}'
        xp = wandb.init(
            name=run_name, project="pracnet", resume="allow", anonymous="allow"
        )

    occupancy = True
    # if expname == "thai_statue":
    #     occupancy = True
    # else:
    #     occupancy = False

    # Load image and scale
    im = io.loadmat("data/%s.mat" % expname)["hypercube"].astype(np.float32)
    im = ndimage.zoom(im / im.max(), [scale, scale, scale], order=0)

    # If the volume is an occupancy, clip to tightest bounding box
    if occupancy:
        hidx, widx, tidx = np.where(im > 0.99)
        im = im[
            hidx.min() : hidx.max(), widx.min() : widx.max(), tidx.min() : tidx.max()
        ]

    print(im.shape)
    H, W, T = im.shape

    maxpoints = min(H * W * T, maxpoints)

    imten = torch.tensor(im).cuda().reshape(H * W * T, 1)

    if nonlin == "posenc":
        nonlin = "relu"
        posencode = True
    else:
        posencode = False

    # Create model
    model = models.get_INR(
        nonlin=nonlin,
        in_features=3,
        out_features=1,
        hidden_features=hidden_features,
        hidden_layers=hidden_layers,
        first_omega_0=omega0,
        hidden_omega_0=omega0,
        scale=sigma0,
        pos_encode=posencode,
        sidelength=max(H, W, T),
    ).cuda()

    # Optimizer
    optim = torch.optim.Adam(lr=learning_rate, params=model.parameters())

    # Schedule to 0.1 times the initial rate
    scheduler = LambdaLR(optim, lambda x: 0.2 ** min(x / niters, 1))

    criterion = torch.nn.MSELoss()

    # Create inputs
    coords = utils.get_coords(H, W, T)

    mse_array = np.zeros(niters)
    time_array = np.zeros(niters)
    best_mse = float("inf")
    best_img = None

    tbar = tqdm.tqdm(range(niters))

    im_estim = torch.zeros((H * W * T, 1), device="cuda")

    tic = time.time()
    print("Running %s nonlinearity" % nonlin)
    for idx in tbar:
        indices = torch.randperm(H * W * T)

        train_loss = 0
        nchunks = 0
        for b_idx in range(0, H * W * T, maxpoints):
            b_indices = indices[b_idx : min(H * W * T, b_idx + maxpoints)]
            b_coords = coords[b_indices, ...].cuda()
            b_indices = b_indices.cuda()
            pixelvalues = model(b_coords[None, ...]).squeeze()[:, None]

            with torch.no_grad():
                im_estim[b_indices, :] = pixelvalues

            loss = criterion(pixelvalues, imten[b_indices, :])

            optim.zero_grad()
            loss.backward()
            optim.step()

            lossval = loss.item()
            train_loss += lossval
            nchunks += 1

        if occupancy:
            mse_array[idx] = volutils.get_IoU(im_estim, imten, mcubes_thres)
        else:
            mse_array[idx] = train_loss / nchunks

        if os.getenv("WANDB_LOG") in ["true", "True", True]:
            xp.log(
                {
                    "loss": train_loss / nchunks,
                    "iou": volutils.get_IoU(im_estim, imten, mcubes_thres),
                }
            )

        time_array[idx] = time.time()
        scheduler.step()

        im_estim_vol = im_estim.reshape(H, W, T)

        if lossval < best_mse:
            best_mse = lossval
            best_img = copy.deepcopy(im_estim)

        if sys.platform == "win32":
            cv2.imshow("GT", im[..., idx % T])
            cv2.imshow("Estim", im_estim_vol[..., idx % T].detach().cpu().numpy())
            cv2.waitKey(1)

        tbar.set_description("%.4e" % mse_array[idx])
        tbar.refresh()

    total_time = time.time() - tic
    nparams = utils.count_parameters(model)

    best_img = best_img.reshape(H, W, T).detach().cpu().numpy()

    if posencode:
        nonlin = "posenc"

    # Save data
    os.makedirs(
        os.path.join(os.getenv("RESULTS_SAVE_PATH"), "%s" % expname),
        exist_ok=True,
    )

    (indices,) = np.where(time_array > 0)
    time_array = time_array[indices]
    mse_array = mse_array[indices]

    mdict = {
        "mse_array": mse_array,
        "time_array": time_array - time_array[0],
        "nparams": utils.count_parameters(model),
    }

    mat_save_path = os.path.join(
        os.getenv("RESULTS_SAVE_PATH"), f"{expname}_{nonlin}.mat"
    )
    io.savemat(
        mat_save_path, mdict
    )

    # Generate a mesh with marching cubes if it is an occupancy volume
    if occupancy:
        volutils.march_and_save(best_img, mcubes_thres, mat_save_path, True)

    print("Total time %.2f minutes" % (total_time / 60))
    if occupancy:
        print("IoU: ", volutils.get_IoU(best_img, im, mcubes_thres))
    else:
        print("PSNR: ", utils.psnr(im, best_img))
    print("Total pararmeters: %.2f million" % (nparams / 1e6))

    # save the model
    os.makedirs(
        os.path.join(os.getenv("MODEL_SAVE_PATH"), "occupancy"),
        exist_ok=True,
    )
    torch.save(
        model.state_dict(),
        os.path.join(
            os.getenv("MODEL_SAVE_PATH"),
            "occupancy",
            "%s.pth" % nonlin,
        ),
    )

    print("saving the mesh on WANDB")
    np.save(mat_save_path, best_img)

    artifact = wandb.Artifact('3d_mesh', type='mesh')
    artifact.add_file(mat_save_path)
    wandb.log_artifact(artifact)
