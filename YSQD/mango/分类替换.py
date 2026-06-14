import pandas as pd
import os
import random
import difflib
import tkinter as tk
from tkinter import filedialog, messagebox
from pathlib import Path
import re


# 违禁关键词库（2025-2026主流平台+海关+电商政策汇总）
PROHIBITED_KEYWORDS = [
    # 武器/管制刀具/爆炸物
    'gun', 'firearm', 'rifle', 'pistol', 'shotgun', 'ammunition', 'bullet', 'magazine', 'silencer', 'suppressor',
    'switchblade', 'automatic knife', 'butterfly knife', 'brass knuckles', 'knuckle duster', 'taser', 'stun gun',
    'explosive', 'bomb', 'grenade', 'detonator', 'molotov', 'c4', 'dynamite', 'rocket', 'missile',
    '枪', '刀具', '爆炸物', '炸弹', '手枪', '步枪', '弹药', '军火',

    # 毒品/管制药品/精神类
    'drug', 'cocaine', 'heroin', 'meth', 'methamphetamine', 'fentanyl', 'marijuana', 'weed', 'cannabis', 'thc',
    'cbd unapproved', 'delta-8', 'delta-9', 'psilocybin', 'lsd', 'ecstasy', 'mdma', 'ketamine', 'steroid', 'anabolic',
    'opioid', 'oxycodone', 'vicodin', 'prescription without', 'rx required', 'pharma restricted', 'clenbuterol',
    'ephedrine', 'pseudoephedrine', 'dmt', 'salvia', 'kratom', 'amanita',
    '毒品', '大麻', '冰毒', '海洛因', '可卡因', '摇头丸', 'K粉', '假药', '管制药品', '精神药品',

    # 假货/侵权/盗版
    'counterfeit', 'fake', 'replica', 'knockoff', 'dupe', 'inspired by', 'look alike', 'copy', 'bootleg', 'pirate',
    'unauthorized', 'trademark violation', 'ip complaint', 'high imitation', '高仿', '假货', '盗版', '山寨',

    # 危险品/禁运化学品（保留了部分高危的，锂电池等视平台政策可再调整）
    'hazardous', 'poison', 'toxic', 'corrosive', 'flammable', 'lithium battery loose', 'lithium ion loose', 'mercury',
    'asbestos', 'pesticide', 'herbicide', 'chemical weapon', 'acid', 'battery restricted',
    '危险品', '腐蚀性', '易燃',

    # 濒危野生动植物制品
    'ivory', 'rhino horn', 'shark fin', 'turtle shell', 'endangered', 'cites', 'wildlife product', 'real fur',
    'exotic leather restricted', 'python skin', 'alligator',
    '象牙', '犀牛角', '鲨鱼鳍', '穿山甲',

    # 虚假医疗/神药/禁宣称
    'cure cancer', 'cure covid', 'miracle cure', 'fda unapproved', 'medical device restricted', 'cpap without',
    'prescription required', '虚假医疗', '包治百病', '神药', '治癌',

    # 其他高风险/道德/法律禁售
    'lottery', 'gambling', 'casino', 'poker chip', 'slot machine', 'lockpick', 'lock pick', 'stolen',
    'illegal activity', '彩票', '赌博', '赌场', '博彩',
    'used underwear', 'human remains', 'human hair unprocessed', 'event ticket restricted', 'embargoed goods',
    'sanctioned country', 'counterfeit money', 'forged', 'swastika', 'nazi', 'forced labour', '强制劳动',
    'child-like sex doll', '违禁品', '禁售', '人体器官', '二手内衣', '人体遗骸', '极端符号'
]


# 随机消息池（100条有趣中文日志）
FUN_MESSAGES = [
    "🚀 起飞啦！文件已经抓到手了～",
    "嘿嘿～这份数据看起来就很香！",
    "📂 文件加载完成，准备开整！",
    "哇塞！又来一份新鲜出炉的数据包～",
    "启动清洗模式……本仙女要发威啦！",
    "文件已就位，今天也要把它们收拾得服服帖帖哦～",
    "侦探上线！开始搜查可疑分子！",
    "🕵️‍♀️ 扫黄打非时间到～谁敢来挑战？",
    "⚠️ 危险物品大搜查，开始！",
    "正在用火眼金睛扫描三列……别想逃！",
    "文件打开成功！准备迎接一场大清洗～",
    "今天的数据宝宝们看起来有点皮哦～",
    "来来来，让我看看你藏了什么秘密～",
    "文件已成功拐回家，开始调教！",
    "启动仪式完成～准备搞事情！",
    "🛡️ 全场扫黄打非开始！标题+分类+描述，一个都别想跑！",
    "正在用放大镜检查……坏东西快现身吧！",
    "发现可疑分子！准备开除籍！",
    "🔥 违禁品大扫荡中……谁敢露头谁倒霉！",
    "干掉 {count} 个坏蛋～干得漂亮！",
    "违禁清理完毕，世界又清净了一点点～",
    "这些家伙也太嚣张了，直接踢出局！",
    "扫荡完成！今天又拯救地球一次～",
    "坏东西一个不留，全部送走！",
    "违禁品已打包带走～再见啦～",
    "扫描完毕！干净得像刚洗过澡一样～",
    "这些违禁词也太明显了吧～一眼就抓到！",
    "清理违禁 {count} 条，效率拉满！",
    "今天的违禁分子集体下线～",
    "扫黄打非小分队汇报：任务完成！",
    "违禁品已就地正法～",
    "发现 {count} 个嫌疑犯，已全部铲除！",
    "坏东西别想藏！全被我揪出来了～",
    "违禁清理完毕，空气都清新了！",
    "今天又除掉 {count} 个祸害～",
    "🧼 分类洗澡时间到！开始搓搓搓～",
    "正在给分类们做SPA～舒服吗？",
    "中文党注意！你们要被无情抛弃啦～",
    "扔掉 {count} 行中文～拜拜不送！",
    "清洗中……让它们变得漂漂亮亮！",
    "分类宝宝们已洗香香～",
    "脏分类已清理干净，准备上桌！",
    "洗完澡的分类看起来都精神多了～",
    "中文行已打包寄走～",
    "清洗完成！数据们现在香喷喷～",
    "给分类们做个大扫除～灰尘全没了！",
    "中文党集体下线～再见～",
    "清洗模式关闭，效果拔群！",
    "分类宝宝们已重获新生～",
    "洗完澡的感觉就是爽！",
    "📊 正在给分类们点名册……",
    "哇！{small} 个小透明在角落瑟瑟发抖～",
    "大佬们有 {large} 个！人气爆棚！",
    "小透明们，来找个好人家吧～",
    "正在帮小分类们安排相亲对象……",
    "匹配中……缘分来了挡不住！",
    "合并完成！大家庭又壮大了～",
    "小透明已成功抱大腿～",
    "统计完毕！数据们都找到组织啦～",
    "大佬分类们继续闪耀～",
    "小分类们已集体转正～",
    "合并仪式完成！鼓掌👏👏👏",
    "低频分类已全部安顿好～",
    "小透明们不再孤单啦～",
    "合并成功！大家一起加油！",
    "统计结果出炉～好看的很！",
    "小分类们已成功上位～",
    "大佬们继续霸榜～",
    "小分类们已成功逆袭～",
    "💾 保存中……新文件快要出生啦！",
    "原文件已灰飞烟灭（手动狗头）",
    "原文件删不掉？没事，新文件才是主角！",
    "🎉 任务完成！本仙女又立功啦～",
    "处理完毕！快去看看你的新宝贝吧😎",
    "大功告成！今天又美美地拯救了数据～",
    "新文件已安全降生～",
    "一切就绪！可以收工了～",
    "任务完成！奖励自己一杯奶茶～",
    "处理结束！数据们都乖乖的～",
    "新文件已打包好～快去抱回家！",
    "今天的数据处理也太顺利了吧～",
    "完成！感觉自己又变聪明了～",
    "文件已完美进化～",
    "收工！今天表现满分～",
    "新文件已就位～期待你的检验！",
    "处理完毕！世界又美好了一点点～",
    "大胜利！🎉🎉🎉",
    "清洗完成！今天的数据真乖～",
    "新文件诞生！快来亲一口～",
    "任务结束！本宝宝要去吃宵夜啦～",
    "数据处理完毕！人生赢麻了～",
    "文件已完美处理～可以骄傲了！",
    "今天的任务又被我轻松拿下～",
    "新文件已准备好迎接主人～",
    "一切就绪！去看看成果吧～",
    "处理完成！心情大好～",
    "数据清洗完毕！爽！",
    "任务达成！可以摸鱼了～",
    "今天又干了一票大的～",
    "新文件已就绪～快来夸我！",
    "处理结束！完美收官～",
    "数据宝宝们已全部安顿好～"
]


def is_prohibited(text: str) -> bool:
    if pd.isna(text) or not text:
        return False
    text_lower = str(text).lower().strip()
    return any(kw in text_lower for kw in PROHIBITED_KEYWORDS)


def choose_excel_file():
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)

    file_path = filedialog.askopenfilename(
        title="请选择要处理的 Excel 文件（需包含 'Categories' 或 '分类' 列）",
        filetypes=[("Excel 文件", "*.xlsx *.xls"), ("所有文件", "*.*")]
    )
    root.destroy()
    return file_path


def clean_for_match(text: str) -> str:
    if pd.isna(text) or not text:
        return ""
    text = str(text).strip().lower()
    for c in ['|||', '|', '>', '->', '→', '-', '–', '—', ':', '/', '\\', '\n', '\r', '\t', '&']:
        text = text.replace(c, ' ')
    text = ' '.join(text.split())
    return text.strip()


def get_best_category_match(small_cat: str, large_cats: list[str]) -> str:
    if not large_cats:
        return "Other"

    small_clean = clean_for_match(small_cat)
    if not small_clean:
        return "Other"

    for large in large_cats:
        if small_clean == clean_for_match(large):
            return large

    best_match = None
    best_score = -1.0
    small_words = set(small_clean.split())
    small_word_count = len(small_words)

    if small_word_count == 0:
        return "Other"

    for large in large_cats:
        large_clean = clean_for_match(large)
        if not large_clean:
            continue

        if small_clean in large_clean or large_clean.startswith(small_clean + ' '):
            return large

        large_words = set(large_clean.split())
        intersection = len(small_words & large_words)
        union = len(small_words | large_words)
        if union == 0:
            continue
        jaccard = intersection / union
        len_penalty = min(small_word_count, len(large_words)) / max(small_word_count, len(large_words))
        score = jaccard * (0.75 + 0.25 * len_penalty)

        if intersection >= 1 and score > best_score:
            best_score = score
            best_match = large

    if best_match and best_score >= 0.40:
        return best_match

    matches = difflib.get_close_matches(small_clean, [clean_for_match(c) for c in large_cats], n=1, cutoff=0.62)
    if matches:
        for large in large_cats:
            if clean_for_match(large) == matches[0]:
                return large

    return "Other"


def clean_category(text):
    if pd.isna(text):
        return "Other"

    text = str(text).strip()
    if not text:
        return "Other"

    text = text.replace('_', ' ')

    cleaned_symbols = ''.join(c for c in text.lower() if c not in ' &|>-:/\\ \t\n\r')
    if not cleaned_symbols or cleaned_symbols.isdigit():
        return "Other"

    common_separators = [
        '>', '->', '→', '-', '–', '—', ':', '/', '\\', '|', '&', '&&', '&amp;',
        '|||', ' ||| ', '||| ', '\n', '\r', '\r\n', '\t', ',', ';'
    ]
    for sep in common_separators:
        text = text.replace(sep, "|||")

    text = "|||".join([p.strip() for p in text.split("|||") if p.strip()])
    text = text.strip("||| ").strip()

    parts = [part.strip() for part in text.split("|||") if part.strip()]

    if not parts:
        return "Other"

    if parts and parts[0].replace('.', '').replace('-', '').isdigit():
        parts = parts[1:]

    garbage = {
        '', '-', '--', '---', 'none', 'null', 'unknown', 'other', 'others',
        'na', 'n/a', 'test', 'demo', 'temp'
    }
    parts = [p for p in parts if p.lower() not in garbage and len(p) > 1]

    if len(parts) == 1 and len(parts[0]) <= 2 and not any(c.isalpha() for c in parts[0]):
        return "Other"

    meaningless_top = {
        "home", "root", "main", "category", "categories", "top", "uncategorized",
        "other", "others", "misc", "miscellaneous", "all", "everything"
    }
    if parts and parts[0].lower() in meaningless_top:
        parts = parts[1:]

    if not parts:
        return "Other"

    has_chinese = any(re.search(r'[\u4e00-\u9fff]', part) for part in parts)
    if has_chinese:
        return "DROP_ROW"

    brand_to_category = {
        "louis vuitton": "Luxury Handbags", "lv": "Luxury Handbags",
        "gucci": "Luxury Handbags & Accessories", "hermes": "Luxury Leather Goods",
        "prada": "Luxury Handbags", "chanel": "Luxury Fashion",
        "dior": "Luxury Fashion & Beauty", "balenciaga": "Luxury Streetwear",
        "saint laurent": "Luxury Fashion", "ysl": "Luxury Fashion",
        "burberry": "Luxury Trench & Fashion", "celine": "Luxury Minimalist Bags",
        "fendi": "Luxury Handbags", "loewe": "Luxury Handbags",
        "bottega veneta": "Luxury Leather Goods", "alexander mcqueen": "Luxury Fashion",
        "miu miu": "Luxury Fashion", "the row": "Luxury Minimalist Fashion",
        "moncler": "Luxury Outerwear", "versace": "Luxury Fashion",
        "rolex": "Luxury Watches", "patek philippe": "Luxury Watches",
        "audemars piguet": "Luxury Watches", "vacheron constantin": "Luxury Watches",
        "richard mille": "Luxury Watches", "jaeger-lecoultre": "Luxury Watches",
        "breitling": "Luxury Watches", "omega": "Luxury Watches",
        "cartier": "Luxury Jewelry & Watches", "tiffany": "Luxury Jewelry",
        "coach": "Mid-Range Handbags", "kate spade": "Mid-Range Handbags",
        "tory burch": "Mid-Range Handbags", "michael kors": "Mid-Range Handbags",
        "furla": "Mid-Range Handbags", "mcm": "Mid-Range Handbags",
        "polene": "Mid-Range Handbags", "demellier": "Mid-Range Handbags",
        "strathberry": "Mid-Range Handbags", "staud": "Mid-Range Handbags",
        "by far": "Mid-Range Handbags", "osoi": "Mid-Range Handbags",
        "savette": "Mid-Range Handbags", "khaite": "Mid-Range Handbags",
        "ganni": "Contemporary Fashion", "nanushka": "Contemporary Fashion",
        "sezane": "Contemporary Fashion", "toteme": "Contemporary Fashion",
        "sandro": "Contemporary Fashion", "maje": "Contemporary Fashion",
        "reformation": "Contemporary Fashion", "everlane": "Contemporary Fashion",
        "melissa & doug": "Mid-Range Educational Toys",
        "fat brain toys": "Mid-Range Educational Toys",
        "little tikes": "Mid-Range Outdoor Toys", "vtech": "Mid-Range Learning Toys",
        "playskool": "Mid-Range Starter Toys", "jellycat": "Mid-Range Plush Toys",
        "pop mart": "Mid-Range Collectible Blind Boxes",
        "bubble mart": "Mid-Range Collectible Blind Boxes",
        "52toys": "Mid-Range Collectible Blind Boxes",
        "sonny angel": "Mid-Range Collectible Figures",
        "labubu": "Mid-Range Collectible Blind Boxes",
        "warmies": "Mid-Range Weighted Plush", "hape": "Mid-Range Wooden Toys",
        "mideer": "Mid-Range Puzzle Toys",
    }

    replaced_parts = []
    for part in parts:
        part_lower = part.lower()
        matched = False
        for brand_key, cat in brand_to_category.items():
            if brand_key in part_lower:
                replaced_parts.append(cat)
                matched = True
                break
        if not matched:
            replaced_parts.append(part)

    if not replaced_parts:
        return "Other"

    return "|||".join(replaced_parts)


def replace_small_categories_in_excel(file_path: str, threshold: int = 10):
    try:
        print(random.choice(FUN_MESSAGES))

        df = pd.read_excel(file_path)

        possible_cat_cols = ["Categories", "分类"]
        category_col = next((col for col in possible_cat_cols if col in df.columns), None)
        if not category_col:
            lower_cols = [c.lower() for c in df.columns]
            if 'categories' in lower_cols:
                category_col = df.columns[lower_cols.index('categories')]
            elif '分类' in df.columns:
                category_col = '分类'
        if not category_col:
            messagebox.showerror("哎呀出错了", "文件里没找到 'Categories' 或 '分类' 列哦～")
            return

        title_candidates = ["Title", "标题", "Product Name", "Name", "商品名称", "商品标题"]
        title_col = next((c for c in title_candidates if c in df.columns), None)

        desc_candidates = ["Description", "描述", "Detail", "Content", "Body", "商品描述", "详情", "Details"]
        desc_col = next((c for c in desc_candidates if c in df.columns), None)

        print(random.choice(FUN_MESSAGES))
        if title_col: print(f"   标题列：'{title_col}'")
        if desc_col: print(f"   描述列：'{desc_col}'")
        print(f"📊 哇，一共有 {len(df):,} 行数据！好多啊～")

        print(random.choice(FUN_MESSAGES))
        prohibited_mask = pd.Series(False, index=df.index)

        if title_col:
            prohibited_mask |= df[title_col].apply(is_prohibited)
        prohibited_mask |= df[category_col].apply(is_prohibited)
        if desc_col:
            prohibited_mask |= df[desc_col].apply(is_prohibited)

        prohibited_count = prohibited_mask.sum()
        if prohibited_count > 0:
            df = df[~prohibited_mask].copy()
            print(random.choice(FUN_MESSAGES).format(count=prohibited_count))

        print(random.choice(FUN_MESSAGES))
        df[category_col] = df[category_col].apply(clean_category)

        drop_mask = df[category_col] == "DROP_ROW"
        dropped_count = drop_mask.sum()
        if dropped_count > 0:
            df = df[~drop_mask].copy()
            print(random.choice(FUN_MESSAGES).format(count=dropped_count))

        print(random.choice(FUN_MESSAGES))
        counts = df[category_col].value_counts(dropna=False)
        small_cats = counts[counts < threshold].index.tolist()
        large_cats = [c for c in counts[counts >= threshold].index.tolist() if c != "Other"]

        print(f"  小透明分类：{len(small_cats):,} 个（太冷门了）")
        print(f"  大佬分类：{len(large_cats):,} 个（人气王）")

        replace_map = {}
        if small_cats:
            print(random.choice(FUN_MESSAGES))
            for cat in small_cats:
                if cat != "Other":
                    replace_map[cat] = get_best_category_match(cat, large_cats)

            df[category_col] = df[category_col].replace(replace_map)

        file_path_obj = Path(file_path)
        output_file = file_path_obj.parent / f"{file_path_obj.stem}_clean.xlsx"
        df.to_excel(output_file, index=False)

        try:
            os.remove(file_path)
            print(random.choice(FUN_MESSAGES))
        except:
            print(random.choice(FUN_MESSAGES))

        print(random.choice(FUN_MESSAGES))
        print(f"📄 新文件诞生：{output_file.name}")
        print(f"   共干掉违禁：{prohibited_count:,} 行 | 扔掉中文：{dropped_count:,} 行")

        messagebox.showinfo("大胜利！", f"处理完成啦！\n新文件：{output_file.name}\n违禁清理：{prohibited_count:,} 条\n中文扔掉：{dropped_count:,} 行\n快去看看成果吧～😎")

    except Exception as e:
        messagebox.showerror("出大事了！", str(e))
        print(f"😭 报错了：{e}")


if __name__ == "__main__":
    excel_path = choose_excel_file()
    if not excel_path:
        print(random.choice(FUN_MESSAGES))
        exit()

    threshold = 30
    replace_small_categories_in_excel(excel_path, threshold)