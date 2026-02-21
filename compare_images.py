import sys
import cv2
import numpy as np
from pathlib import Path


#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
直接修改路径并运行以比较图片相似度
已修正：处理负值相关性和SSIM，确保输出在 [0, 1] 范围内
"""

# ================================
# 在这里修改图片路径
IMAGE_PATH_1 = "temp_thumbnails\\error_case\\thumb_52637823130148_1770210359974.jpg"
IMAGE_PATH_2 = "deleted.jpg"
# ================================

def compare_images(image_path1: str, image_path2: str) -> dict:
    """
    比较两张图片的相似度
    
    参数:
        image_path1: 第一张图片路径
        image_path2: 第二张图片路径
        
    返回:
        dict: 包含各种相似度指标的字典，结果保证在 [0, 1] 之间
    """
    # 读取图片 - 使用 cv2.imdecode 支持中文路径
    img1 = cv2.imdecode(np.fromfile(image_path1, dtype=np.uint8), cv2.IMREAD_COLOR)
    img2 = cv2.imdecode(np.fromfile(image_path2, dtype=np.uint8), cv2.IMREAD_COLOR)
    
    if img1 is None:
        raise ValueError(f"无法读取第一张图片: {image_path1}")
    if img2 is None:
        raise ValueError(f"无法读取第二张图片: {image_path2}")
    
    # 统一尺寸（使用较小的尺寸，保持比例）
    h1, w1 = img1.shape[:2]
    h2, w2 = img2.shape[:2]
    
    target_size = (min(w1, w2), min(h1, h2))
    img1_resized = cv2.resize(img1, target_size)
    img2_resized = cv2.resize(img2, target_size)
    
    # 1. 均方误差（MSE）
    mse = np.mean((img1_resized.astype(float) - img2_resized.astype(float)) ** 2)
    # MSE 转换为相似度 (0-1)，假设 10000 为极大差异
    mse_sim = max(0.0, 1.0 - (mse / 10000.0))
    
    # 2. 直方图比较
    hsv1 = cv2.cvtColor(img1_resized, cv2.COLOR_BGR2HSV)
    hsv2 = cv2.cvtColor(img2_resized, cv2.COLOR_BGR2HSV)
    hist_bins = [50, 60, 60]
    hist_ranges = [0, 180, 0, 256, 0, 256]
    
    hist1 = cv2.calcHist([hsv1], [0, 1, 2], None, hist_bins, hist_ranges)
    hist2 = cv2.calcHist([hsv2], [0, 1, 2], None, hist_bins, hist_ranges)
    
    cv2.normalize(hist1, hist1, 0, 1, cv2.NORM_MINMAX)
    cv2.normalize(hist2, hist2, 0, 1, cv2.NORM_MINMAX)
    
    # 修正：相关性可能为负，将其截断在 [0, 1]
    correlation = cv2.compareHist(hist1, hist2, cv2.HISTCMP_CORREL)
    correlation_sim = max(0.0, correlation) 
    
    bhattacharyya = cv2.compareHist(hist1, hist2, cv2.HISTCMP_BHATTACHARYYA)
    bhattacharyya_sim = max(0.0, 1.0 - bhattacharyya)
    
    # 3. 特征匹配
    try:
        orb = cv2.ORB_create(nfeatures=500)
        kp1, des1 = orb.detectAndCompute(img1_resized, None)
        kp2, des2 = orb.detectAndCompute(img2_resized, None)
        
        if des1 is not None and des2 is not None and len(des1) > 0 and len(des2) > 0:
            bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
            matches = bf.match(des1, des2)
            matches = sorted(matches, key=lambda x: x.distance)
            good_matches = [m for m in matches if m.distance < 50]
            match_ratio = min(1.0, len(good_matches) / max(len(kp1), len(kp2))) if max(len(kp1), len(kp2)) > 0 else 0
        else:
            match_ratio = 0
    except Exception:
        match_ratio = 0
    
    # 4. 结构相似度 SSIM
    ssim_score = None
    ssim_sim = 0.0
    try:
        from skimage.metrics import structural_similarity as ssim
        gray1 = cv2.cvtColor(img1_resized, cv2.COLOR_BGR2GRAY)
        gray2 = cv2.cvtColor(img2_resized, cv2.COLOR_BGR2GRAY)
        ssim_raw = ssim(gray1, gray2)
        # 修正：SSIM 可能为负，截断在 [0, 1]
        ssim_sim = max(0.0, ssim_raw)
        ssim_score = ssim_raw
    except ImportError:
        pass
    
    # 综合评分（加权）
    if ssim_score is not None:
        overall_sim = (
            correlation_sim * 0.3 +
            bhattacharyya_sim * 0.2 +
            mse_sim * 0.2 +
            match_ratio * 0.1 +
            ssim_sim * 0.2
        )
    else:
        overall_sim = (
            correlation_sim * 0.35 +
            bhattacharyya_sim * 0.25 +
            mse_sim * 0.25 +
            match_ratio * 0.15
        )
    
    # 兜底确保最终结果不溢出范围
    overall_sim = max(0.0, min(1.0, overall_sim))
    
    return {
        "mse": mse,
        "mse_similarity": mse_sim,
        "histogram_correlation": correlation,
        "histogram_correlation_sim": correlation_sim,
        "histogram_bhattacharyya_similarity": bhattacharyya_sim,
        "feature_match_ratio": match_ratio,
        "ssim": ssim_score,
        "overall_similarity": overall_sim
    }


def print_results(results: dict, img1_path: str, img2_path: str):
    """打印比较结果"""
    print("\n" + "=" * 60)
    print(f"  图片相似度比较结果 (已修正负值处理)")
    print("=" * 60)
    print(f"  图片1: {img1_path}")
    print(f"  图片2: {img2_path}")
    print("-" * 60)
    print(f"  均方误差 (MSE):              {results['mse']:.2f}")
    print(f"  MSE 相似度:                  {results['mse_similarity']:.4f}")
    print(f"  直方图相关性 (原始):          {results['histogram_correlation']:.4f}")
    print(f"  直方图相关性 (修正后):        {results['histogram_correlation_sim']:.4f}")
    print(f"  直方图巴氏相似度:            {results['histogram_bhattacharyya_similarity']:.4f}")
    print(f"  特征点匹配率:                {results['feature_match_ratio']:.4f}")
    if results['ssim'] is not None:
        print(f"  SSIM 结构相似度:             {results['ssim']:.4f}")
    print("-" * 60)
    print(f"  综合相似度:                  {results['overall_similarity']:.4f}")
    print(f"  相似度百分比:                {results['overall_similarity']*100:.2f}%")
    print("=" * 60)
    
    sim = results['overall_similarity']
    if sim >= 0.95:
        print("  评价: 图片几乎完全相同")
    elif sim >= 0.85:
        print("  评价: 图片高度相似")
    elif sim >= 0.70:
        print("  评价: 图片比较相似")
    elif sim >= 0.50:
        print("  评价: 图片有一定相似度")
    else:
        print("  评价: 图片差异较大")
    print("=" * 60 + "\n")


def main():
    if IMAGE_PATH_1.strip() and IMAGE_PATH_2.strip():
        img_path1, img_path2 = IMAGE_PATH_1, IMAGE_PATH_2
    elif len(sys.argv) == 3:
        img_path1, img_path2 = sys.argv[1], sys.argv[2]
    else:
        print("\n用法: python compare_images.py <图片1路径> <图片2路径>\n")
        sys.exit(1)
    
    if not Path(img_path1).exists() or not Path(img_path2).exists():
        print(f"错误: 图片路径不存在")
        sys.exit(1)
    
    try:
        results = compare_images(img_path1, img_path2)
        print_results(results, img_path1, img_path2)
    except Exception as e:
        print(f"\n比较图片时出错: {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
