#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一日志配置模块
"""

import logging
import os
from datetime import datetime


def setup_logger(name=None, log_file=None, level=logging.INFO):
    """
    设置统一的日志配置
    
    Args:
        name: 日志名称
        log_file: 日志文件路径，默认会在当前目录生成带时间戳的日志文件
        level: 日志级别，默认 INFO
    
    Returns:
        logging.Logger: 配置好的日志对象
    """
    # 创建日志记录器
    logger = logging.getLogger(name)
    logger.setLevel(level)
    
    # 防止重复添加处理器
    if not logger.handlers:
        # 日志格式：时间 | 级别 | 模块 | 消息
        formatter = logging.Formatter(
            '%(asctime)s | %(levelname)-8s | %(module)-15s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # 控制台处理器
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
        
        # 文件处理器
        if log_file:
            log_dir = os.path.dirname(log_file)
            if log_dir and not os.path.exists(log_dir):
                os.makedirs(log_dir)
        else:
            # 默认日志文件路径
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            log_file = f'logs/{timestamp}_app.log'
            log_dir = os.path.dirname(log_file)
            if log_dir and not os.path.exists(log_dir):
                os.makedirs(log_dir)
        
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    
    return logger


# 创建默认日志记录器
default_logger = setup_logger()
