class QMDSException(Exception):
    """项目基础异常"""


class ScrapeError(QMDSException):
    """爬取过程通用错误"""


class DetectionError(ScrapeError):
    """平台检测失败"""


class ExtractionError(ScrapeError):
    """数据提取失败"""


class ProxyError(ScrapeError):
    """代理相关错误"""


class RateLimitError(ScrapeError):
    """被目标限速"""
