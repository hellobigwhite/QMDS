DB_PATH = "station_clients.db"
TABLE_NAME = "sites"
REPORT_STATUS_COL = "report_status"
REPORT_TIME_COL = "report_time"
DOMAIN_RESOLVED_TIME_COL = "domain_resolved_time"
SCHEDULE_ENABLED_COL = "schedule_enabled"
SCHEDULE_TIME_COL = "schedule_time"
DOMAIN_NUMBER_COL = "domain_number"
BUILD_STATUS_COL = "build_status"
BUILD_TIME_COL = "build_time"
MEDIA_STATUS_COL = "media_status"
MEDIA_TIME_COL = "media_time"
HEALTH_STATUS_COL = "health_status"
HEALTH_TIME_COL = "health_time"
PLUGIN_STATUS_COL = "plugin_status"
PLUGIN_TIME_COL = "plugin_time"
MAIN_DATA_STATUS_COL = "main_data_status"
MAIN_DATA_TIME_COL = "main_data_time"
AUTO_CATEGORY_STATUS_COL = "auto_category_status"
AUTO_CATEGORY_TIME_COL = "auto_category_time"
EXTRA_DATA_STATUS_COL = "extra_data_status"
EXTRA_DATA_TIME_COL = "extra_data_time"
MAIN_CATEGORY_STATUS_COL = "main_category_status"
MAIN_CATEGORY_TIME_COL = "main_category_time"
AUTO_WORKFLOW_ENABLED_COL = "auto_workflow_enabled"
AUTO_WORKFLOW_STEP_COL = "auto_workflow_step"
AUTO_WORKFLOW_STATUS_COL = "auto_workflow_status"
AUTO_WORKFLOW_RETRY_COUNT_COL = "auto_workflow_retry_count"
AUTO_WORKFLOW_MAX_RETRY_COL = "auto_workflow_max_retry"

EXTRA_COLUMNS = [
    "classification",
    "build_flag",
    "title_translation",
    "description_translation",
    "main_keyword",
    "long_tail_keywords",
    "report_id",
    "domain_status",
    "login_path",
    REPORT_TIME_COL,
    DOMAIN_RESOLVED_TIME_COL,
    SCHEDULE_ENABLED_COL,
    SCHEDULE_TIME_COL,
    DOMAIN_NUMBER_COL,
    BUILD_STATUS_COL,
    BUILD_TIME_COL,
    MEDIA_STATUS_COL,
    MEDIA_TIME_COL,
    HEALTH_STATUS_COL,
    HEALTH_TIME_COL,
    PLUGIN_STATUS_COL,
    PLUGIN_TIME_COL,
    MAIN_DATA_STATUS_COL,
    MAIN_DATA_TIME_COL,
    AUTO_CATEGORY_STATUS_COL,
    AUTO_CATEGORY_TIME_COL,
    MAIN_CATEGORY_STATUS_COL,
    MAIN_CATEGORY_TIME_COL,
    EXTRA_DATA_STATUS_COL,
    EXTRA_DATA_TIME_COL,
    AUTO_WORKFLOW_ENABLED_COL,
    AUTO_WORKFLOW_STEP_COL,
    AUTO_WORKFLOW_STATUS_COL,
    AUTO_WORKFLOW_RETRY_COUNT_COL,
    AUTO_WORKFLOW_MAX_RETRY_COL,
]

COLUMNS = [
    ("domain", "域名"),
    ("template", "模板"),
    ("main_data_source_id", "主数据源ID"),
    ("extra_data_source_id", "补充数据源ID"),
    ("main_category", "主打类目"),
    ("category", "大类"),
    ("title", "SEO Title（最大58字符）"),
    ("description", "Meta Description"),
    ("address", "地址"),
    ("store_pf", "盘符"),
    ("server", "服务器"),
    ("logo", "Logo"),
    ("banner", "Banner"),
    ("icon", "Icon"),
]

EXCEL_COLS = [chr(ord("A") + i) for i in range(len(COLUMNS))]

CATEGORY_OPTIONS = [
    "五金",
    "交通工具",
    "体育用品",
    "保健",
    "办公用品",
    "动物",
    "商业",
    "婴幼儿用品",
    "媒体",
    "宗教",
    "家具",
    "家居与园艺",
    "成人",
    "服饰与配饰",
    "玩具",
    "电子产品",
    "相机与光学器件",
    "箱包",
    "艺术与娱乐",
    "软件",
    "饮食",
]

CATEGORY_ID_MAP = {
    "五金": "1",
    "交通工具": "2",
    "体育用品": "3",
    "保健": "4",
    "办公用品": "5",
    "动物": "6",
    "商业": "7",
    "婴幼儿用品": "8",
    "媒体": "9",
    "宗教": "10",
    "家具": "11",
    "家居与园艺": "12",
    "成人": "13",
    "服饰与配饰": "14",
    "玩具": "15",
    "电子产品": "16",
    "相机与光学器件": "17",
    "箱包": "18",
    "艺术与娱乐": "19",
    "软件": "20",
    "饮食": "21",
}

DOMAIN_STATUS_LABELS = {
    None: "未买",
    "": "未买",
    1: "新增",
    "1": "新增",
    2: "已购买",
    "2": "已购买",
    3: "已解析",
    "3": "已解析",
    4: "已建站",
    "4": "已建站",
}
