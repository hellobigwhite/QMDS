
import os
import webbrowser
import time
from datetime import datetime
import threading
from pathlib import Path
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy import create_engine, Column, Integer, String, Text, ForeignKey, Table
from sqlalchemy import DateTime
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, Session
from sqlalchemy.sql import or_
from typing import List, Optional
from fastapi import Depends, HTTPException, Query
from fastapi import BackgroundTasks
from pydantic import BaseModel

from Other import splice_path, process_domain
from DataCrawler import shopify_main
from DataHandle import pretreatment_main
from DataSource import ai_api_gemin



# 初始化 FastAPI
app = FastAPI()

# 允许跨域请求
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许所有域访问
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 配置 SQLite 数据库
DATABASE_URL = "sqlite:///./local_source_data.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# 数据源与产品标签关联表（多对多关系），添加级联删除
data_source_tags = Table(
    "data_source_tags",
    Base.metadata,
    Column("data_source_id", Integer, ForeignKey("data_sources.id", ondelete="CASCADE")),
    Column("tag_id", Integer, ForeignKey("tags.id", ondelete="CASCADE"))
)

# 数据源管理模型
class DataSource(Base):
    __tablename__ = "data_sources"
    
    id = Column(Integer, primary_key=True, index=True)  # ID
    created_date = Column(DateTime, nullable=False, default=datetime.utcnow)  # 创建日期
    url = Column(String(255), nullable=False, unique=True)  # 网址，添加唯一性约束
    site_name = Column(String(255), nullable=True)  # 站点名称
    site_title = Column(Text, nullable=True)  # 站点标题
    site_describe = Column(Text, nullable=True)  # 站点描述
    site_language = Column(String(50), nullable=True)  # 站点语言
    site_currency = Column(String(10), nullable=True)  # 站点币种
    site_techstack = Column(Text, nullable=True)  # 站点技术栈
    ai_analysis_summary = Column(Text, nullable=True)  # AI 分析总结
    product_volume = Column(Integer, nullable=False, default=0)  # 产品数量
    data_volume = Column(Integer, nullable=False, default=0)  # 数据量
    analysis_result = Column(Text, nullable=True)  # 数据分析结果
    remark = Column(Text, nullable=True)  # 备注
    status = Column(String(50), nullable=True)  # 状态
    custom_field_1 = Column(Text, nullable=True)  # 预留字段 1
    custom_field_2 = Column(Text, nullable=True)  # 预留字段 2
    tags = relationship("Tags", secondary=data_source_tags, back_populates="data_sources")  # 关联标签，表示数据源与标签的多对多关系

# 产品标签模型
class Tags(Base):
    __tablename__ = "tags"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, unique=True)
    data_sources = relationship("DataSource", secondary=data_source_tags, back_populates="tags")
    
    @property
    def count(self):
        return len(self.data_sources)

# 创建数据库表
Base.metadata.create_all(bind=engine)

# 依赖项: 获取数据库会话
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

#--------------------------------------------
# 挂载静态文件目录
# 将原来的/static挂载点改为/assets
app.mount("/assets", StaticFiles(directory="DataSource/static/dist/assets"), name="assets")

@app.get("/")
async def serve_frontend():
    return FileResponse("DataSource/static/dist/index.html")

#--------------------------------------------
# 增删改查（CRUD）操作
#--------------------------------------------

# 数据源 Pydantic 模型，用于请求和响应
class DataSourceBase(BaseModel):
    url: str
    site_name: Optional[str] = None
    site_title: Optional[str] = None
    site_describe: Optional[str] = None
    site_language: Optional[str] = None
    site_currency: Optional[str] = None
    site_techstack: Optional[str] = None
    ai_analysis_summary: Optional[str] = None
    product_volume: int = 0
    data_volume: int = 0
    analysis_result: Optional[str] = None
    remark: Optional[str] = None
    status: Optional[str] = None
    custom_field_1: Optional[str] = None
    custom_field_2: Optional[str] = None
    tags: Optional[str] = None  # 修改这里，接收前端传入的逗号分割字符串

class DataSourceCreate(DataSourceBase):
    pass

class DataSourceUpdate(DataSourceBase):
    id: int

class DataSourceResponse(DataSourceBase):
    id: int
    created_date: datetime

    class Config:
        from_attributes = True


class TagResponse(BaseModel):
    id: int
    name: str
    count: int

    class Config:
        from_attributes = True

class DataSourceResponse(BaseModel):
    id: int
    url: str
    site_name: Optional[str] = None
    site_title: Optional[str] = None
    site_describe: Optional[str] = None
    site_language: Optional[str] = None
    site_currency: Optional[str] = None
    site_techstack: Optional[str] = None
    ai_analysis_summary: Optional[str] = None
    product_volume: int = 0
    data_volume: int = 0
    analysis_result: Optional[str] = None
    remark: Optional[str] = None
    status: Optional[str] = None
    custom_field_1: Optional[str] = None
    custom_field_2: Optional[str] = None
    created_date: datetime
    tags: List[TagResponse] = []  # 返回标签详情

    class Config:
        from_attributes = True

class DataSourceListResponse(BaseModel):
    total: int
    items: List[DataSourceResponse]


# 批量更新的更新数据模型，所有字段均设为 Optional
class DataSourceUpdateData(BaseModel):
    url: Optional[str] = None
    site_name: Optional[str] = None
    site_title: Optional[str] = None
    site_describe: Optional[str] = None
    site_language: Optional[str] = None
    site_currency: Optional[str] = None
    site_techstack: Optional[str] = None
    ai_analysis_summary: Optional[str] = None
    product_volume: Optional[int] = None
    data_volume: Optional[int] = None
    analysis_result: Optional[str] = None
    remark: Optional[str] = None
    status: Optional[str] = None
    custom_field_1: Optional[str] = None
    custom_field_2: Optional[str] = None
    tags: Optional[str] = None

# 批量更新请求模型
class DataSourceBatchUpdateRequest(BaseModel):
    ids: List[int]
    update_data: DataSourceUpdateData


#--------------------------------------------
# 数据源 CRUD API 端点
#--------------------------------------------
@app.post("/data_sources/", response_model=DataSourceResponse)
def create_data_source(data_source: DataSourceCreate, db: Session = Depends(get_db)):
    url = data_source.url.strip()  # 去除首尾空白
    if not url:
        raise HTTPException(status_code=400, detail="URL不能为空")
    existing_data_source = db.query(DataSource).filter(DataSource.url == url).first()
    if existing_data_source:
        raise HTTPException(status_code=400, detail="Data source URL already exists")
    
    data = data_source.dict(exclude={"tags"})
    data["url"] = url
    db_data_source = DataSource(**data)
    
    if data_source.tags:
        # 解析标签字符串，逗号分割，并生成标签列表
        tag_names = [name.strip() for name in data_source.tags.split(',') if name.strip()]
        tag_instances = []
        for name in tag_names:
            tag_instance = db.query(Tags).filter(Tags.name == name).first()
            if not tag_instance:
                # 标签不存在则创建
                tag_instance = Tags(name=name)
                db.add(tag_instance)
                db.commit()
                db.refresh(tag_instance)
            tag_instances.append(tag_instance)
        db_data_source.tags = tag_instances
    
    db.add(db_data_source)
    db.commit()
    db.refresh(db_data_source)
    return db_data_source


@app.get("/data_sources/", response_model=DataSourceListResponse)
def read_data_sources(
    db: Session = Depends(get_db),
    skip: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=100),
    techstack: Optional[str] = Query(None),
    language: Optional[str] = Query(None),
    currency: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    tags: Optional[List[int]] = Query(None),
    product_volume_min: Optional[int] = Query(None),
    product_volume_max: Optional[int] = Query(None),
    data_volume_min: Optional[int] = Query(None),
    data_volume_max: Optional[int] = Query(None),
    search: Optional[str] = Query(None),
):
    query = db.query(DataSource)

    if techstack:
        query = query.filter(DataSource.site_techstack == techstack)
    if language:
        query = query.filter(DataSource.site_language == language)
    if currency:
        query = query.filter(DataSource.site_currency == currency)
    if status:
        query = query.filter(DataSource.status == status)
    if tags:
        query = query.filter(DataSource.tags.any(Tags.id.in_(tags)))
    if product_volume_min is not None:
        query = query.filter(DataSource.product_volume >= product_volume_min)
    if product_volume_max is not None:
        query = query.filter(DataSource.product_volume <= product_volume_max)
    if data_volume_min is not None:
        query = query.filter(DataSource.data_volume >= data_volume_min)
    if data_volume_max is not None:
        query = query.filter(DataSource.data_volume <= data_volume_max)

    if search:
        search_term = f"%{search}%"
        query = query.filter(
            or_(
                DataSource.url.like(search_term),
                DataSource.site_name.like(search_term),
                DataSource.site_title.like(search_term),
                DataSource.site_describe.like(search_term),
                DataSource.ai_analysis_summary.like(search_term),
                DataSource.analysis_result.like(search_term),
                DataSource.remark.like(search_term)
            )
        )

    if hasattr(DataSource, "created_date"):
        query = query.order_by(DataSource.created_date.desc())

    # 计算总数
    total = query.count()

    # 分页查询
    data_sources = query.offset(skip).limit(limit).all()

    return {"total": total, "items": data_sources}

@app.get("/data_sources/{data_source_id}", response_model=DataSourceResponse)
def read_data_source(data_source_id: int, db: Session = Depends(get_db)):
    db_data_source = db.query(DataSource).filter(DataSource.id == data_source_id).first()
    if db_data_source is None:
        raise HTTPException(status_code=404, detail="Data source not found")
    return db_data_source

@app.put("/data_sources/", response_model=DataSourceResponse)
def update_data_source(data_source: DataSourceUpdate, db: Session = Depends(get_db)):
    db_data_source = db.query(DataSource).filter(DataSource.id == data_source.id).first()
    if db_data_source is None:
        raise HTTPException(status_code=404, detail="Data source not found")
    
    url = data_source.url.strip()  # 去除首尾空白
    if not url:
        raise HTTPException(status_code=400, detail="URL不能为空")
    
    existing_data_source = db.query(DataSource).filter(DataSource.url == url, DataSource.id != data_source.id).first()
    if existing_data_source:
        raise HTTPException(status_code=400, detail="Data source URL already exists")
    
    for key, value in data_source.dict(exclude={"id", "tags"}).items():
        if key == "url":
            setattr(db_data_source, key, url)
        else:
            setattr(db_data_source, key, value)
    
    if data_source.tags:
        tag_names = [name.strip() for name in data_source.tags.split(',') if name.strip()]
        tag_instances = []
        for name in tag_names:
            tag_instance = db.query(Tags).filter(Tags.name == name).first()
            if not tag_instance:
                tag_instance = Tags(name=name)
                db.add(tag_instance)
                db.commit()
                db.refresh(tag_instance)
            tag_instances.append(tag_instance)
        db_data_source.tags = tag_instances
    else:
        # 如果传入的 tags 为空，可以考虑清空关联
        db_data_source.tags = []
    
    db.commit()
    db.refresh(db_data_source)
    return db_data_source


# 批量修改端点
@app.put("/data_sources/batch_update/")
def batch_update_data_sources(request: DataSourceBatchUpdateRequest, db: Session = Depends(get_db)):
    updated_items = []
    for ds_id in request.ids:
        db_data_source = db.query(DataSource).filter(DataSource.id == ds_id).first()
        if not db_data_source:
            continue  # 或者记录下不存在的id，返回给前端提示

        # 对每个字段进行更新
        for key, value in request.update_data.dict(exclude_unset=True).items():
            if key != "tags":
                setattr(db_data_source, key, value)
            else:
                # 对 tags 字段进行单独处理
                if value:
                    tag_names = [name.strip() for name in value.split(',') if name.strip()]
                    tag_instances = []
                    for name in tag_names:
                        tag_instance = db.query(Tags).filter(Tags.name == name).first()
                        if not tag_instance:
                            tag_instance = Tags(name=name)
                            db.add(tag_instance)
                            db.commit()
                            db.refresh(tag_instance)
                        tag_instances.append(tag_instance)
                    db_data_source.tags = tag_instances
                else:
                    db_data_source.tags = []
        updated_items.append(db_data_source)
    db.commit()
    return {"updated": len(updated_items), "ids": request.ids}


@app.delete("/data_sources/{data_source_id}")
def delete_data_source(data_source_id: int, db: Session = Depends(get_db)):
    db_data_source = db.query(DataSource).filter(DataSource.id == data_source_id).first()
    if db_data_source is None:
        raise HTTPException(status_code=404, detail="Data source not found")
    db.delete(db_data_source)
    db.commit()
    return {"ok": True}



#--------------------------------------------
# 产品标签 Pydantic 模型
#--------------------------------------------
class TagBase(BaseModel):
    name: str

class TagCreate(TagBase):
    pass

class TagUpdate(TagBase):
    id: int
    name: str

# class TagResponse(TagBase):
#     id: int

#     class Config:
#         from_attributes = True



# 新增分页返回的模型
class TagListResponse(BaseModel):
    data: List[TagResponse]
    total: int

#--------------------------------------------
# 产品标签 CRUD API 端点
#--------------------------------------------
@app.post("/tags/", response_model=TagResponse)
def create_tag(tag: TagCreate, db: Session = Depends(get_db)):
    tag_name = tag.name.strip()  # 去除首尾空白
    existing_tag = db.query(Tags).filter(Tags.name == tag_name).first()
    if existing_tag:
        raise HTTPException(status_code=400, detail="Tag name already exists")
    db_tag = Tags(name=tag_name)
    db.add(db_tag)
    db.commit()
    db.refresh(db_tag)
    return db_tag


@app.get("/tags/", response_model=TagListResponse)
def read_tags(db: Session = Depends(get_db), page: int = 1, pageSize: int = 10, search: Optional[str] = Query(None)):
    skip = (page - 1) * pageSize
    query = db.query(Tags)
    if search:
        query = query.filter(Tags.name.like(f"%{search}%"))
    total = query.count()
    tags = query.order_by(Tags.id.desc()).offset(skip).limit(pageSize).all()
    return {"data": tags, "total": total}

@app.get("/tags/{tag_id}", response_model=TagResponse)
def read_tag(tag_id: int, db: Session = Depends(get_db)):
    db_tag = db.query(Tags).filter(Tags.id == tag_id).first()
    if db_tag is None:
        raise HTTPException(status_code=404, detail="Tag not found")
    return db_tag

@app.put("/tags/", response_model=TagResponse)
def update_tag(tag: TagUpdate, db: Session = Depends(get_db)):
    db_tag = db.query(Tags).filter(Tags.id == tag.id).first()
    if db_tag is None:
        raise HTTPException(status_code=404, detail="Tag not found")
    tag_name = tag.name.strip()  # 去除首尾空白
    existing_tag = db.query(Tags).filter(Tags.name == tag_name, Tags.id != tag.id).first()
    if existing_tag:
        raise HTTPException(status_code=400, detail="Tag name already exists")
    db_tag.name = tag_name
    db.commit()
    db.refresh(db_tag)
    return db_tag

@app.delete("/tags/{tag_id}")
def delete_tag(tag_id: int, db: Session = Depends(get_db)):
    db_tag = db.query(Tags).filter(Tags.id == tag_id).first()
    if db_tag is None:
        raise HTTPException(status_code=404, detail="Tag not found")
    db.delete(db_tag)
    db.commit()
    return {"ok": True}


#------------------------------------------------------------------------
# 定义后台任务请求数据模型，包含任务名称和待处理的数据源 id 列表
class BackgroundTaskRequest(BaseModel):
    task_name: str = "默认任务"
    ids: List[int]

#------------------------------------------------------------------------
# 后台耗时任务函数 模板
# """
# 查询到的数据源信息打印示例：{'id': 1, 'url': 'https://fitfiltration.co.uk', 'site_name': 'Fit Filtration', 'site_title': '', 'site_describe': 'Custom made aquariums and metal frame aquarium stands made in Sheffield, Marine shop selling dry goods fish corals and inverts, also selling large amounts of natural sea water salt water around the UK with delivery available.', 'site_language': '', 'site_currency': 'GBP', 'site_techstack': 'Shopify', 'ai_analysis_summary': '', 'product_volume': 1102, 'data_volume': 0, 'analysis_result': '', 'remark': '', 'status': '已采集', 'custom_field_1': None, 'custom_field_2': None, 'created_date': datetime.datetime(2025, 3, 15, 8, 52, 36, 935856)}
# """
# def process_ids_task(task_name: str, ids: List[int]):
#     print(f"后台任务开始：{task_name}, 处理数据源 IDs: {ids}")
#     # 模拟耗时任务，例如睡眠 10 秒
#     time.sleep(10)
#     # 更新数据库中对应 id 的状态为 “已采集”，并查询打印全部信息
#     db = SessionLocal()
#     try:
#         for ds_id in ids:
#             db_data_source = db.query(DataSource).filter(DataSource.id == ds_id).first()
#             if db_data_source:
#                 # 查询对应id的全部信息（示例打印所有字段）
#                 data_info = {
                    # "id": db_data_source.id,
                    # "url": db_data_source.url,
                    # "site_name": db_data_source.site_name,
                    # "site_title": db_data_source.site_title,
                    # "site_describe": db_data_source.site_describe,
                    # "site_language": db_data_source.site_language,
                    # "site_currency": db_data_source.site_currency,
                    # "site_techstack": db_data_source.site_techstack,
                    # "ai_analysis_summary": db_data_source.ai_analysis_summary,
                    # "product_volume": db_data_source.product_volume,
                    # "data_volume": db_data_source.data_volume,
                    # "analysis_result": db_data_source.analysis_result,
                    # "remark": db_data_source.remark,
                    # "status": db_data_source.status,
                    # "custom_field_1": db_data_source.custom_field_1,
                    # "custom_field_2": db_data_source.custom_field_2,
                    # "created_date": db_data_source.created_date,
#                 }
#                 print(f"查询到的数据源信息：{data_info}")
#                 # 更新状态为“已采集”
#                 db_data_source.status = "已采集"
#                 print(f"更新数据源 {ds_id} 状态为 已采集")
#         db.commit()
#     except Exception as e:
#         db.rollback()
#         print("更新失败:", e)
#     finally:
#         db.close()
#     print(f"后台任务结束：{task_name}, 处理完成")

# # 后台任务端口：接收前端提交的任务请求，并添加后台任务
# @app.post("/background_task/")
# async def background_task_endpoint(payload: BackgroundTaskRequest, background_tasks: BackgroundTasks):
#     print("收到后台任务请求，数据:", payload)
#     if not payload.ids:
#         raise HTTPException(status_code=400, detail="未传入待处理的数据源ID")
#     # 添加后台任务，传入任务名称和 id 列表
#     background_tasks.add_task(process_ids_task, payload.task_name, payload.ids)
#     return {"message": "任务已提交，后台执行中..."}


#------------------------------------------------------------------------
# 一键SP采集后台任务
def process_ids_task_sp(task_name: str, ids: List[int]):
    print(f"后台任务开始：{task_name}, 处理数据源 IDs: {ids}")
    db = SessionLocal()
    try:
        for ds_id in ids:
            try:
                db_data_source = db.query(DataSource).filter(DataSource.id == ds_id).first()
                if not db_data_source:
                    print(f"数据源ID {ds_id} 不存在，跳过。")
                    continue

                # 查询对应id的部分信息，重点关注技术栈
                data_info = {
                    "id": db_data_source.id,
                    "url": db_data_source.url,
                    "site_techstack": db_data_source.site_techstack,
                    "status": db_data_source.status,
                }
                print(f"查询到的数据源信息：{data_info}")
                
                # 采集开始前先检查技术栈是否为 "Shopify"
                if db_data_source.site_techstack.lower() != "shopify":
                    print(f"数据源 {ds_id} 的技术栈不是 Shopify，跳过采集。")
                    continue

                # 获取数据源对应的清理后的域名
                cleaned_url = process_domain.run(db_data_source.url)
                print(f"开始采集 {db_data_source.url} 的数据...")

                # 创建保存目录：当前脚本目录下的 Data 文件夹，再根据完整域名创建子文件夹
                base_output_dir = os.path.join(os.getcwd(), "Data")
                output_dir = os.path.join(base_output_dir, cleaned_url)

                if not os.path.exists(base_output_dir):
                    os.makedirs(base_output_dir)
                    print(f"目录 {base_output_dir} 已创建。")
                if not os.path.exists(output_dir):
                    os.makedirs(output_dir)
                    print(f"目录 {output_dir} 已创建。")
                
                # 调用采集核心函数一次，返回结果作为是否采集成功的判断
                result = shopify_main.run(cleaned_url, output_dir)
                if result:
                    print(f"采集完成，数据已保存到 {output_dir}")
                    db_data_source.status = "已采集"
                    print(f"更新数据源 {ds_id} 状态为 已采集")
                else:
                    print(f"数据源 {ds_id} 采集失败")
                
                # 每个数据源处理完成后立即提交事务
                db.commit()
            except Exception as e:
                # 单个数据源处理出现异常时回滚当前事务，记录错误信息后继续下一个数据源
                db.rollback()
                print(f"数据源 {ds_id} 处理失败，错误信息: {e}")
                continue
    except Exception as e:
        print("任务失败:", e)
    finally:
        db.close()
    print(f"后台任务结束：{task_name}")

#------------------------------------------------------------------------
# 一键SP数据清洗后台任务
def process_ids_task_sp_clean(task_name: str, ids: List[int]):
    # 打印任务开始的提示信息，显示任务名称和待处理的数据源 ID 列表
    print(f"后台任务开始：{task_name}, 处理数据源 IDs: {ids}")
    
    # 创建数据库会话
    db = SessionLocal()
    try:
        # 遍历每个待处理的数据源 ID
        for ds_id in ids:
            try:
                # 查询数据库中对应数据源 ID 的记录
                db_data_source = db.query(DataSource).filter(DataSource.id == ds_id).first()
                if not db_data_source:
                    # 如果没有查询到对应的数据源，则打印提示并跳过当前循环
                    print(f"数据源ID {ds_id} 不存在，跳过。")
                    continue

                # 从查询到的数据中提取部分关键信息（重点关注技术栈）
                data_info = {
                    "id": db_data_source.id,
                    "url": db_data_source.url,
                    "site_name": db_data_source.site_name,
                    "site_techstack": db_data_source.site_techstack,
                    "data_volume": db_data_source.data_volume,
                    "status": db_data_source.status,
                    "custom_field_1": db_data_source.custom_field_1,

                }
                print(f"查询到的数据源信息：{data_info}")
                
                # 在采集前先检查数据源的技术栈是否为 "Shopify"
                if db_data_source.site_techstack.lower() != "shopify":
                    print(f"数据源 {ds_id} 的技术栈不是 Shopify，跳过采集。")
                    continue

                # 使用 process_domain 模块处理原始 URL，获取清理后的域名
                cleaned_url = process_domain.run(db_data_source.url)
                print(f"开始采集 {db_data_source.url} 的数据...")

                # 构造保存数据的目录路径：
                # 当前脚本目录下的 Data 文件夹，然后根据清理后的完整域名创建子文件夹
                base_output_dir = os.path.join(os.getcwd(), "Data")
                input_path = os.path.join(base_output_dir, cleaned_url, "data.xlsx")
                
                # 如果文件不存在，则打印提示信息，并跳过当前数据源的处理
                if not os.path.exists(input_path):
                    print(f"文件 {input_path} 不存在，跳过数据源 {ds_id}。")
                    continue

                # 提取 URL 的主域名，用于后续数据清洗处理
                domain = cleaned_url.split('/')[0]

                # 站点名称（直接从数据源记录中获取）
                site_name = db_data_source.site_name

                # 自定义分类名称（中文），用于填充数据中空白的自定义分类列
                category_name = db_data_source.custom_field_1

                # 自定义分类（英文），用于填充数据中空白的 Categories 列
                custom_category = ""

                # 检查 category_name、site_name 和 domain 是否为空
                if not site_name or not category_name or not domain:
                    print(f"数据源 {ds_id} 存在空字段：site_name: {site_name}, category_name: {category_name}, domain: {domain}，跳过数据清洗。")
                    continue

                # 变体处理方式设置（SP专用处理策略）
                process_variants = 2

                # 站点标识符，可用于标识不同的数据来源
                site_identifier = 0

                # 数据语言设置
                language = "en"

                # 调用通用数据清洗工具核心函数 pretreatment_main.run
                # 参数依次为：文件路径、自定义分类（英文）、自定义分类名称（中文）、站点名称、域名、变体处理方式、站点标识符、语言
                result = pretreatment_main.run(input_path, custom_category, category_name, site_name, domain, process_variants, site_identifier, language)
                if result:
                    # 如果数据清洗成功，打印成功提示并更新数据库中数据源的状态为 "已清洗"
                    print(f"采集完成，数据已保存")
                    db_data_source.status = "已清洗"
                    print(f"更新数据源 {ds_id} 状态为 已清洗")
                else:
                    # 如果数据清洗失败，打印失败提示
                    print(f"数据源 {ds_id} 清洗失败")
                
                # 每处理完一个数据源后，立即提交事务，确保数据库状态及时更新
                db.commit()
            except Exception as e:
                # 单个数据源处理出现异常时，回滚当前事务，并打印错误信息后继续处理下一个数据源
                db.rollback()
                print(f"数据源 {ds_id} 处理失败，错误信息: {e}")
                continue
    except Exception as e:
        # 如果整个任务出现异常，则打印任务失败信息
        print("任务失败:", e)
    finally:
        # 关闭数据库会话，释放资源
        db.close()
    
    # 打印任务结束的提示信息
    print(f"后台任务结束：{task_name}")

#------------------------------------------------------------------------
def process_ids_task_open_folders(task_name: str, ids: List[int]):
    # 打印任务开始提示
    print(f"后台任务开始：{task_name}, 处理数据源 IDs: {ids}")
    
    # 创建数据库会话
    db = SessionLocal()
    try:
        for ds_id in ids:
            try:
                # 查询数据源记录
                db_data_source = db.query(DataSource).filter(DataSource.id == ds_id).first()
                if not db_data_source:
                    print(f"数据源ID {ds_id} 不存在，跳过。")
                    continue

                # 打印查询到的数据源信息（这里只提取了 id 和 url）
                data_info = {
                    "id": db_data_source.id,
                    "url": db_data_source.url,
                }
                print(f"查询到的数据源信息：{data_info}")
                
                # 使用 process_domain 模块处理原始 URL，获取清理后的 URL
                cleaned_url = process_domain.run(db_data_source.url)
                print(f"获取到数据源 {ds_id} 的清理后 URL: {cleaned_url}")

                # 构造对应目录文件夹路径：当前脚本所在目录下的 Data 文件夹中的子目录
                base_output_dir = os.path.join(os.getcwd(), "Data")
                folder_path = os.path.join(base_output_dir, cleaned_url)
                print(f"对应目录文件夹路径: {folder_path}")

                # 判断该目录是否存在且为文件夹
                if not os.path.exists(folder_path) or not os.path.isdir(folder_path):
                    print(f"目录 {folder_path} 不存在或不是文件夹，跳过数据源 {ds_id}。")
                    continue

                # 尝试打开目录文件夹（Windows平台下可用 os.startfile，如其他平台需使用对应命令）
                try:
                    os.startfile(folder_path)
                    print(f"成功打开目录：{folder_path}")
                except Exception as e:
                    print(f"无法打开目录 {folder_path}，错误信息: {e}")
            except Exception as e:
                # 单个数据源处理出现异常时回滚当前事务，打印错误后继续处理下一个
                db.rollback()
                print(f"数据源 {ds_id} 处理失败，错误信息: {e}")
                continue
    except Exception as e:
        print("任务失败:", e)
    finally:
        # 关闭数据库会话，释放资源
        db.close()
    
    # 打印任务结束提示
    print(f"后台任务结束：{task_name}")

#------------------------------------------------------------------------
def process_ids_task_ai_summary(task_name: str, ids: List[int]):
    """
    后台任务：对指定数据源进行 AI 分析，并将总结结果保存到数据库字段 ai_analysis_summary 中。
    
    参数:
        task_name: 任务名称
        ids: 需要处理的数据源ID列表
    """
    print(f"后台任务开始：{task_name}, 处理数据源 IDs: {ids}")
    db = SessionLocal()
    try:
        for ds_id in ids:
            try:
                db_data_source = db.query(DataSource).filter(DataSource.id == ds_id).first()
                if not db_data_source:
                    print(f"数据源ID {ds_id} 不存在，跳过。")
                    continue

                # 查询对应ID的部分信息，只获取 id 和 url
                data_info = {
                    "id": db_data_source.id,
                    "url": db_data_source.url,
                }

                print(f"查询到的数据源信息：{data_info}")
                
                # 调用AI函数进行网站分析
                print(f"调用AI进行网站分析，URL: {db_data_source.url}")

                content = db_data_source.url

                prompt = "analyze_site"

                ai_result = ai_api_gemin.gemini_call(content, prompt)

                print(f"AI分析结果: {ai_result}")
                
                # 更新数据源的 ai_analysis_summary 字段，将 AI 返回的内容写入
                db_data_source.ai_analysis_summary = ai_result
                print(f"更新数据源 {ds_id} 的 AI总结信息")
                
                # 每个数据源处理完成后立即提交事务
                db.commit()

                # 在处理完每个数据源后加入延迟
                print(f"数据源 {ds_id} 处理完成，等待延迟...")
                time.sleep(3)  # 这里设置延迟时间为1秒，可以根据需要调整
            except Exception as e:
                # 单个数据源处理异常时回滚当前事务，记录错误后继续下一个数据源
                db.rollback()
                print(f"数据源 {ds_id} 处理失败，错误信息: {e}")
                continue
    except Exception as e:
        print("任务失败:", e)
    finally:
        db.close()
    print(f"后台任务结束：{task_name}")



def process_ids_task_ai_tagging(task_name: str, ids: List[int]):
    """
    后台任务：对指定数据源进行 AI 分析，并将总结结果作为产品标签保存。
    
    参数:
        task_name: 任务名称
        ids: 需要处理的数据源ID列表
    """
    print(f"后台任务开始：{task_name}, 处理数据源 IDs: {ids}")
    db = SessionLocal()
    try:
        for ds_id in ids:
            try:
                db_data_source = db.query(DataSource).filter(DataSource.id == ds_id).first()
                if not db_data_source:
                    print(f"数据源ID {ds_id} 不存在，跳过。")
                    continue

                # 查询对应ID的部分信息，只获取 id 和 url
                data_info = {
                    "id": db_data_source.id,
                    "url": db_data_source.url,
                }
                print(f"查询到的数据源信息：{data_info}")
                
                # 调用AI函数进行网站分析
                print(f"调用AI进行网站分析，URL: {db_data_source.url}")
                content = db_data_source.url
                prompt = "summary_tags"
                ai_result = ai_api_gemin.gemini_call(content, prompt)
                print(f"AI分析结果: {ai_result}")
                
                # 如果返回的AI结果包含markdown格式的```json块，则先去除它们
                ai_result_clean = ai_result.strip()
                if ai_result_clean.startswith("```json"):
                    lines = ai_result_clean.splitlines()
                    if lines[-1].strip() == "```":
                        ai_result_clean = "\n".join(lines[1:-1]).strip()
                    else:
                        ai_result_clean = ai_result_clean.replace("```json", "").strip()
                    #print(f"去除markdown格式后的AI结果: {ai_result_clean}")
                
                # 尝试解析AI返回的JSON格式数据，获取产品标签列表（最多5个，标签尽量概括）
                try:
                    import json
                    result_json = json.loads(ai_result_clean)
                    tags = result_json.get("产品类目", [])
                    # 限制标签数量最多10个，并去除标签两端的空白
                    tags = [tag.strip() for tag in tags[:10] if tag.strip()]
                    print(f"解析出的产品标签: {tags}")
                except Exception as e:
                    print(f"解析AI结果失败: {e}")
                    tags = []
                
                # 根据解析出的标签更新数据源的产品标签信息
                tag_instances = []
                for name in tags:
                    tag_instance = db.query(Tags).filter(Tags.name == name).first()
                    if not tag_instance:
                        tag_instance = Tags(name=name)
                        db.add(tag_instance)
                        db.commit()
                        db.refresh(tag_instance)
                    tag_instances.append(tag_instance)
                db_data_source.tags = tag_instances
                print(f"更新数据源 {ds_id} 的产品标签信息: {','.join(tags)}")
                
                # 每个数据源处理完成后立即提交事务
                db.commit()
                # 在处理完每个数据源后加入延迟
                print(f"数据源 {ds_id} 处理完成，等待延迟...")
                time.sleep(3)  # 这里设置延迟时间为1秒，可以根据需要调整
            except Exception as e:
                # 单个数据源处理异常时回滚当前事务，记录错误后继续下一个数据源
                db.rollback()
                print(f"数据源 {ds_id} 处理失败，错误信息: {e}")
                continue
    except Exception as e:
        print("任务失败:", e)
    finally:
        db.close()
    print(f"后台任务结束：{task_name}")


#------------------------------------------------------------------------

# 定义任务映射关系
TASK_MAPPING = {
    "批量打开数据目录": process_ids_task_open_folders,
    "一键SP采集": process_ids_task_sp,
    "一键SP数据清洗": process_ids_task_sp_clean,
    "AI总结": process_ids_task_ai_summary,
    "AI标签": process_ids_task_ai_tagging,
    # 如果有其他任务，可以在这里添加
}

# 统一后台任务入口
@app.post("/background_task/")
async def background_task_entry(payload: BackgroundTaskRequest, background_tasks: BackgroundTasks):
    print("收到后台任务请求，数据:", payload)
    
    if not payload.task_name or not payload.ids:
        raise HTTPException(status_code=400, detail="任务名称或数据源ID缺失")

    # 根据任务名称获取对应的任务函数
    task_function = TASK_MAPPING.get(payload.task_name)
    if not task_function:
        raise HTTPException(status_code=400, detail=f"未知的任务名称: {payload.task_name}")

    # 添加后台任务
    background_tasks.add_task(task_function, payload.task_name, payload.ids)
    return {"message": f"任务 '{payload.task_name}' 已提交，后台执行中..."}




# -----------------------------------------------------------------------
def open_browser():
    # 等待服务器启动
    time.sleep(2)
    webbrowser.open("http://127.0.0.1:8050")

if __name__ == "__main__":
    # 开启一个线程来打开浏览器
    threading.Thread(target=open_browser).start()
    import uvicorn
    uvicorn.run("DataSource.run:app", host="127.0.0.1", port=8050, reload=True)
    
