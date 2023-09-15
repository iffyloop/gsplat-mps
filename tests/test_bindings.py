"""Test against ref bindings

Make sure you have the ref bindings installed:
    - clone the ref bindings: git clone --recurse-submodules git@github.com:graphdeco-inria/diff-gaussian-rasterization.git
    - install ref bindings: pip install -e .
"""

import math
import os
from typing import Optional

import torch
from diff_gaussian_rasterization import (
    _C,
    GaussianRasterizationSettings,
    GaussianRasterizer,
)
from diff_rast import project_gaussians, rasterize
from torch import Tensor

device = torch.device("cuda:0")
NUM_POINTS = 100
fx, fy = 1.0, 1.0
H, W = 256, 256
GLOBAL_SCALE = 1
BLOCK_X, BLOCK_Y = 16, 16
TILE_BOUNDS = (W + BLOCK_X - 1) // BLOCK_X, (H + BLOCK_Y - 1) // BLOCK_Y, 1


def test_bindings_forward(save_img=False):
    means, scales, quats, rgbs, opacities, viewmat, projmat = _init_gaussians()

    ref_color, saved_tensors = _run_ref(
        means3D=means,
        colors_precomp=rgbs,
        opacities=opacities,
        scales=scales,
        rotations=quats,
        viewmat=viewmat,
        projmat=projmat,
        cov3Ds_precomp=None,
    )
    ref_color_display = ref_color.permute(
        1, 2, 0
    )  # out color is (3,H,W)

    color = _run_diff_rast(
        means=means.clone(), rgbs=rgbs.clone(), scales=scales.clone(), opacities=opacities.clone(), quats=quats.clone(), viewmat=viewmat, projmat=projmat
    )

    if save_img:
        _save_img(color, os.getcwd() + f"/ours.png")
        _save_img(ref_color_display, os.getcwd() + f"/ref.png")

    torch.testing.assert_close(color, ref_color_display)
    # Zhuoyang's Note: I tried to take the backward check out, but it seems it could take a while to make it work.
    # So I feel it is better to keep it here for now (and it might be useful for debugging after the forward pass works)
    
    ref_grads = _run_ref_backward(saved_tensors, ref_color)
    diff_grads = _run_diff_rast_backward(color, means, rgbs, opacities, scales, quats)
    
    for ref_grad, diff_grad in zip(ref_grads, diff_grads):
        torch.testing.assert_close(ref_grad.shape, diff_grad.shape)


def _init_gaussians():
    means = torch.randn((NUM_POINTS, 3), device=device)
    scales = torch.randn((NUM_POINTS, 3), device=device)
    quats = torch.randn((NUM_POINTS, 4), device=device)
    quats /= torch.linalg.norm(quats, dim=-1, keepdim=True)
    viewmat = torch.eye(4, device=device)
    projmat = torch.eye(4, device=device)
    rgbs = torch.rand((NUM_POINTS, 3), device=device)
    opacities = torch.ones(NUM_POINTS, device=device) * 0.9

    means.requires_grad = True
    scales.requires_grad = True
    quats.requires_grad = True
    rgbs.requires_grad = True
    opacities.requires_grad = True
    viewmat.requires_grad = True

    return means, scales, quats, rgbs, opacities, viewmat, projmat


def _setup_ref_settings(viewmat: Tensor, projmat: Tensor):
    tanfovx = 0.5 * W / fx
    tanfovy = 0.5 * H / fy
    campos = viewmat[:3, 3]  # camera center position
    sh_degree = 0

    # background TODO: check that our implementation treats bg in a similar way
    bg = torch.ones((1, 3), dtype=torch.float32, device=device)

    ref_settings = GaussianRasterizationSettings(
        image_height=H,
        image_width=W,
        tanfovx=tanfovx,
        tanfovy=tanfovy,
        bg=bg,
        scale_modifier=GLOBAL_SCALE,
        viewmatrix=viewmat,
        projmatrix=projmat,
        sh_degree=sh_degree,
        campos=campos,
        prefiltered=False,
        debug=True,
    )
    return ref_settings


def _run_ref(
    means3D: Tensor,
    colors_precomp: Tensor,
    opacities: Tensor,
    scales: Tensor,
    rotations: Tensor,
    cov3Ds_precomp: Optional[Tensor],
    viewmat: Tensor,
    projmat: Tensor,
):
    raster_settings = _setup_ref_settings(viewmat=viewmat, projmat=projmat)
    sh = torch.Tensor([]).to(device)  # This sets SH to None, we use precomp_colors

    if cov3Ds_precomp is None:
        cov3Ds_precomp = torch.Tensor([]).to(device)

    args = (
        raster_settings.bg,
        means3D,
        colors_precomp,
        opacities,
        scales,
        rotations,
        raster_settings.scale_modifier,
        cov3Ds_precomp,
        raster_settings.viewmatrix,
        raster_settings.projmatrix,
        raster_settings.tanfovx,
        raster_settings.tanfovy,
        raster_settings.image_height,
        raster_settings.image_width,
        sh,
        raster_settings.sh_degree,
        raster_settings.campos,
        raster_settings.prefiltered,
        raster_settings.debug,
    )

    num_rendered, color, radii, geomBuffer, binningBuffer, imgBuffer = _C.rasterize_gaussians(*args)
    
    saved_tensors = (
        colors_precomp, means3D, scales, rotations, cov3Ds_precomp, radii, sh, geomBuffer, binningBuffer, imgBuffer, num_rendered, raster_settings
    )

    # actual bindings are these, but I think it is better to call the cuda version to get additional outs
    # rasterizer = GaussianRasterizer(raster_settings=raster_settings)
    # color, radii = rasterizer(
    #    means3D=means3D,
    #    means2D=None,
    #    opacities=opacities,
    #    shs=None,
    #    colors_precomp=colors_precomp,
    #    scales=scales,
    #    rotations=quats,
    #    cov3D_precomp=None,
    # )
    return color, saved_tensors

def _run_ref_backward(saved_tensors, grad_out_color):
    colors_precomp, means3D, scales, rotations, cov3Ds_precomp, radii, sh, geomBuffer, binningBuffer, imgBuffer, num_rendered, raster_settings = saved_tensors
    
    args = (raster_settings.bg,
                means3D, 
                radii, 
                colors_precomp, 
                scales, 
                rotations, 
                raster_settings.scale_modifier, 
                cov3Ds_precomp, 
                raster_settings.viewmatrix, 
                raster_settings.projmatrix, 
                raster_settings.tanfovx, 
                raster_settings.tanfovy, 
                grad_out_color, 
                sh, 
                raster_settings.sh_degree, 
                raster_settings.campos,
                geomBuffer,
                num_rendered,
                binningBuffer,
                imgBuffer,
                raster_settings.debug)
   
    grad_means2D, grad_colors_precomp, grad_opacities, grad_means3D, grad_cov3Ds_precomp, grad_sh, grad_scales, grad_rotations = _C.rasterize_gaussians_backward(*args)
    
    grads = (grad_means3D, grad_colors_precomp, grad_opacities, grad_scales, grad_rotations) #, grad_means2D, grad_cov3Ds_precomp, grad_sh)
    
    return grads


def _run_diff_rast(means, rgbs, opacities, scales, quats, viewmat, projmat):
    xys, depths, radii, conics, num_tiles_hit = project_gaussians(
        means, scales, GLOBAL_SCALE, quats, viewmat, projmat, fx, fy, H, W, TILE_BOUNDS
    )
    out_img = rasterize(xys, depths, radii, conics, num_tiles_hit, rgbs, opacities, H, W)

    return out_img

def _run_diff_rast_backward(grad_out_color, means, rgbs, opacities, scales, quats):
    
    grad_out_color.backward(grad_out_color)

    grads = (means.grad, rgbs.grad, opacities.grad.unsqueeze(-1), scales.grad, quats.grad)
    
    return grads

def _save_img(image, image_path):
    from pathlib import Path

    import numpy as np
    from PIL import Image

    if torch.is_tensor(image):
        image = image.detach().cpu().numpy() * 255
        image = image.astype(np.uint8)
    if not Path(os.path.dirname(image_path)).exists():
        Path(os.path.dirname(image_path)).mkdir()
    im = Image.fromarray(image)
    print("saving to: ", image_path)
    im.save(image_path)


if __name__ == "__main__":
    test_bindings_forward(save_img=True)