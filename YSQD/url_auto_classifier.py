import json
import os
import re
import time
from datetime import datetime
from urllib.parse import urlparse

import requests
from pymongo import MongoClient


MONGO_URI = "mongodb://localhost:27017/"
MONGO_DB_NAME = "shopify_url"
BIGDATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mango", "config", "bigdata.json")
ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
EXTRA_DOMAIN_KEYWORDS = {
    "music": [
        "ukulele",
        "mandolin",
        "guitarshop",
        "musicmall",
        "musicshop",
        "instrument",
        "instruments",
        "violin",
        "cello",
        "drum",
        "percussion",
        "clarinet",
    ],
    "toy": [
        "hobby",
        "gunpla",
        "modelkit",
        "figurine",
        "collectible",
        "collectibles",
        "bjd",
        "doll",
        "dolls",
        "rccar",
        "actionfigure",
    ],
    "pet": [
        "paw",
        "paws",
        "pup",
        "puppy",
        "doghouse",
        "dogcrate",
        "cattree",
        "catcondo",
        "canine",
        "feline",
        "petcare",
        "petplus",
    ],
    "sports": [
        "sport",
        "fitness",
        "gym",
        "athletic",
        "soccer",
        "football",
        "basketball",
        "tennis",
        "golf",
        "yoga",
        "pilates",
        "running",
        "hiking",
        "camping",
        "fishing",
        "cycling",
        "bike",
        "ski",
        "snowboard",
        "surf",
        "skate",
        "skateboard",
        "swim",
        "diving",
        "boxing",
        "martialarts",
        "climbing",
        "equestrian",
        "rugby",
        "hockey",
        "cricket",
        "baseball",
        "softball",
        "volleyball",
        "badminton",
        "pickleball",
        "exercise",
        "workout",
        "training",
        "activewear",
        "sportswear",
    ],
}
EXTRA_STRONG_PHRASES = {
    "garden": [
        "garden",
        "planter",
        "planters",
        "seed",
        "seeds",
        "greenhouse",
        "fertilizer",
        "compost",
        "hydroponic",
        "sprinkler",
    ],
    "music": [
        "ukulele",
        "mandolin",
        "guitar",
        "cello",
        "violin",
        "clarinet",
        "cymbals",
        "bass drum",
        "percussion",
        "musical instruments",
    ],
    "toy": [
        "hobby",
        "model kit",
        "gunpla",
        "bjd",
        "collectible doll",
        "collectibles",
        "dolls",
        "figurine",
        "rc car",
        "anime figure",
        "hot toys figure",
    ],
    "pet": [
        "pet shop",
        "pet store",
        "pet supplies",
        "dog food",
        "cat food",
        "dog treats",
        "cat treats",
        "dog crate",
        "cat tree",
        "dog bed",
        "cat bed",
        "dog harness",
        "dog leash",
        "cat litter",
    ],
    "bag": [
        "backpack",
        "tote bag",
        "shoulder bag",
        "messenger bag",
        "briefcase",
        "handbag",
        "travel backpack",
        "suitcase",
        "luggage",
        "duffel bag",
    ],
    "auto": [
        "brake pad",
        "brake rotor",
        "oil filter",
        "air filter",
        "spark plug",
        "shock absorber",
        "control arm",
        "wheel bearing",
        "fuel pump",
        "alternator",
    ],
    "sports": [
        "soccer ball",
        "football",
        "basketball",
        "tennis racket",
        "golf club",
        "yoga mat",
        "dumbbell",
        "kettlebell",
        "treadmill",
        "exercise bike",
        "camping tent",
        "fishing rod",
        "bicycle",
        "bike helmet",
        "snowboard",
        "skateboard",
        "boxing gloves",
        "punching bag",
        "climbing harness",
        "sports equipment",
        "fitness equipment",
        "gym equipment",
        "outdoor gear",
    ],
}
CATEGORY_IGNORED_PHRASES = {
    "religious": {
        "gifts",
        "gift",
        "decor",
        "jewelry",
        "blessed",
        "faith",
        "holy",
        "spiritual",
        "books",
    },
    "electronics": {
        "accessories",
        "storage",
        "battery",
        "charger",
        "cable",
        "speaker",
        "audio",
        "video",
        "phone battery",
        "laptop battery",
        "camera battery",
    },
    "pet": {
        "bedding",
        "storage",
        "accessories",
    },
    "garden": {
        "home decor",
        "wall decor",
        "tabletop decor",
        "lighting",
        "textiles",
        "seasonal decor",
        "kitchen decoration",
        "cookware",
        "kitchen gadgets",
        "table lamp",
        "floor lamp",
        "desk lamp",
        "wall shelf",
        "floating shelf",
        "christmas ornament",
        "votive candle",
        "wine glass",
        "storage",
        "storage cabinet",
        "conditioner",
        "shampoo",
        "bath brush",
        "dinnerware",
        "grill",
        "lantern",
        "napkin",
        "bedding",
    },
    "Office": {
        "printer",
        "scanner",
        "monitor",
        "keyboard",
        "mouse",
        "projector",
        "speaker",
        "headphones",
        "hdmi cable",
        "usb cable",
        "ink cartridge",
        "printer ink",
        "toner cartridge",
        "computer monitor",
    },
    "toy": {
        "game",
        "games",
        "activity toy",
        "learning game",
        "educational game",
    },
    "art_entertainment": {
        "guitar",
        "cello",
        "violin",
        "clarinet",
        "cymbals",
        "bass drum",
        "musical instruments",
        "instrument keyboard",
        "piano",
        "accordion",
        "concertina",
        "flute",
        "saxophone",
        "harmonica",
        "mandolin",
        "ukulele",
    },
    "auto": {
        "battery",
        "spring",
        "chain",
        "mirror",
        "fork",
        "storage",
    },
}


def _normalize_text(value):
    text = str(value or "").strip().lower()
    text = re.sub(r"[^\w\s&'-]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _compact_text(value):
    return re.sub(r"[^a-z0-9]+", "", _normalize_text(value))


def _normalize_url(url):
    raw = str(url or "").strip()
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    parsed = urlparse(raw)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    if not netloc:
        return ""
    return f"{scheme}://{netloc}"


def _extract_domain(url):
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _contains_arabic(value):
    return bool(ARABIC_RE.search(str(value or "")))


def _extract_all_phrases(data, result=None):
    if result is None:
        result = set()
    if isinstance(data, dict):
        for key, value in data.items():
            norm_key = _normalize_text(key)
            if norm_key:
                result.add(norm_key)
            _extract_all_phrases(value, result)
    elif isinstance(data, list):
        for item in data:
            _extract_all_phrases(item, result)
    elif isinstance(data, str):
        norm_value = _normalize_text(data)
        if norm_value:
            result.add(norm_value)
    return result


def load_category_rules():
    with open(BIGDATA_PATH, "r", encoding="utf-8") as file:
        raw = json.load(file)

    rules = {}
    for category_name, config in raw.items():
        domain_keywords = list(config.get("domain_keywords") or [])
        domain_keywords.extend(EXTRA_DOMAIN_KEYWORDS.get(category_name, []))
        categories_data = config.get("categories") or {}
        phrase_source = categories_data.get(category_name, categories_data)
        phrases = sorted(_extract_all_phrases(phrase_source))
        phrases.extend([_normalize_text(item) for item in EXTRA_STRONG_PHRASES.get(category_name, []) if item])
        ignored_phrases = {_normalize_text(item) for item in CATEGORY_IGNORED_PHRASES.get(category_name, set()) if item}
        rules[category_name] = {
            "domain_keywords": [kw for kw in (_normalize_text(x) for x in domain_keywords) if kw],
            "domain_keywords_compact": [kw for kw in (_compact_text(x) for x in domain_keywords) if kw],
            "phrases": [phrase for phrase in phrases if phrase],
            "phrases_compact": [_compact_text(phrase) for phrase in phrases if phrase],
            "strong_phrases": [_normalize_text(item) for item in EXTRA_STRONG_PHRASES.get(category_name, []) if item],
            "strong_phrases_compact": [_compact_text(item) for item in EXTRA_STRONG_PHRASES.get(category_name, []) if item],
            "ignored_phrases": ignored_phrases,
            "ignored_phrases_compact": {_compact_text(item) for item in ignored_phrases if item},
        }
    return rules


class ShopifyAutoClassifier:
    def __init__(self, min_score=14, min_margin=6, max_collection_pages=4, progress_callback=None):
        self.min_score = int(min_score)
        self.min_margin = int(min_margin)
        self.max_collection_pages = int(max_collection_pages)
        self.progress_callback = progress_callback
        self.rules = load_category_rules()
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/133.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Cache-Control": "max-age=0",
            }
        )

    def _log(self, message):
        if self.progress_callback:
            self.progress_callback(message)

    def _fetch_json(self, url, timeout=20, max_retries=2):
        for attempt in range(max_retries):
            try:
                response = self._session.get(url, timeout=timeout)
                response.raise_for_status()
                return response.json(), response.status_code, False
            except requests.HTTPError as exc:
                status_code = getattr(getattr(exc, "response", None), "status_code", 0) or 0
                if status_code == 403 and attempt < max_retries - 1:
                    time.sleep(1)
                    continue
                return None, status_code, False
            except requests.exceptions.SSLError:
                try:
                    response = self._session.get(url, timeout=timeout, verify=False)
                    response.raise_for_status()
                    return response.json(), response.status_code, False
                except requests.HTTPError as exc2:
                    status_code = getattr(getattr(exc2, "response", None), "status_code", 0) or 0
                    return None, status_code, False
                except Exception:
                    if attempt < max_retries - 1:
                        time.sleep(1)
                        continue
                    return None, 0, True
            except Exception:
                if attempt < max_retries - 1:
                    time.sleep(1)
                    continue
                return None, 0, True

    def _fetch_meta(self, base_url):
        payload, status_code, had_error = self._fetch_json(f"{base_url}/meta.json", timeout=12)
        if isinstance(payload, dict):
            return payload, status_code, had_error
        return None, status_code, had_error

    def _fetch_collections(self, base_url):
        items = []
        seen_handles = set()
        had_error = False
        for page in range(1, self.max_collection_pages + 1):
            payload, status_code, request_error = self._fetch_json(
                f"{base_url}/collections.json?limit=250&page={page}",
                timeout=15,
            )
            had_error = had_error or request_error
            if not isinstance(payload, dict):
                if page == 1:
                    payload, status_code, request_error = self._fetch_json(
                        f"{base_url}/collections.json",
                        timeout=15,
                    )
                    had_error = had_error or request_error
                if not isinstance(payload, dict):
                    break

            collections = payload.get("collections")
            if not isinstance(collections, list) or not collections:
                break

            new_count = 0
            for item in collections:
                handle = str(item.get("handle") or "").strip()
                title = str(item.get("title") or "").strip()
                if not handle and not title:
                    continue
                key = handle or title
                if key in seen_handles:
                    continue
                seen_handles.add(key)
                items.append({"handle": handle, "title": title})
                new_count += 1

            if new_count == 0:
                break
            time.sleep(0.15)
        return items, had_error

    def _build_signals(self, url):
        base_url = _normalize_url(url)
        if not base_url:
            return None

        domain = _extract_domain(base_url)
        meta, _meta_status, meta_error = self._fetch_meta(base_url)
        collections, collections_error = self._fetch_collections(base_url)
        had_request_error = meta_error or collections_error
        is_shopify = bool(meta or collections)

        meta_text = ""
        raw_meta_text = ""
        if meta:
            raw_meta_text = " ".join([str(meta.get("name", "")), str(meta.get("description", ""))])
            meta_text = _normalize_text(raw_meta_text)

        raw_collection_titles = [str(item.get("title") or "") for item in collections if item.get("title")]
        collection_titles = [_normalize_text(title) for title in raw_collection_titles if title]
        collection_handles = [_compact_text(item.get("handle")) for item in collections if item.get("handle")]
        has_arabic_content = _contains_arabic(raw_meta_text) or any(_contains_arabic(title) for title in raw_collection_titles)

        return {
            "base_url": base_url,
            "domain": domain,
            "domain_compact": _compact_text(domain),
            "is_shopify": is_shopify,
            "meta_text": meta_text,
            "collection_titles": collection_titles,
            "collection_handles": collection_handles,
            "collection_count": len(collections),
            "had_request_error": had_request_error,
            "has_arabic_content": has_arabic_content,
        }

    def _score_category(self, signals, category_name, rule):
        score = 0
        matched_signals = []

        domain_compact = signals["domain_compact"]
        meta_text = signals["meta_text"]
        collection_titles = signals["collection_titles"]
        collection_handles = signals["collection_handles"]

        domain_hits = []
        for keyword in rule["domain_keywords_compact"]:
            if len(keyword) >= 4 and keyword in domain_compact:
                domain_hits.append(keyword)
        if domain_hits:
            score += min(12, 8 + (len(domain_hits) - 1) * 2)
            matched_signals.append(f"domain:{', '.join(domain_hits[:3])}")

        ignored_phrases = rule.get("ignored_phrases", set())
        ignored_phrases_compact = rule.get("ignored_phrases_compact", set())
        meta_hits = []
        for phrase in rule["phrases"]:
            if phrase in ignored_phrases:
                continue
            if len(phrase) >= 4 and phrase in meta_text:
                meta_hits.append(phrase)
        if meta_hits:
            score += min(12, len(meta_hits[:3]) * 4)
            matched_signals.append(f"meta:{', '.join(meta_hits[:3])}")

        title_hits = []
        for phrase in rule["phrases"]:
            if phrase in ignored_phrases:
                continue
            if len(phrase) < 4:
                continue
            if any(phrase in title for title in collection_titles):
                title_hits.append(phrase)
        if title_hits:
            score += min(18, len(title_hits[:3]) * 6)
            matched_signals.append(f"title:{', '.join(title_hits[:3])}")

        handle_hits = []
        for phrase in rule["phrases_compact"]:
            if not phrase:
                continue
            if phrase in ignored_phrases_compact:
                continue
            if len(phrase) < 5:
                continue
            if any(phrase in handle for handle in collection_handles):
                handle_hits.append(phrase)
        if handle_hits:
            score += min(9, len(handle_hits[:3]) * 3)
            matched_signals.append(f"handle:{', '.join(handle_hits[:3])}")

        strong_meta_hits = []
        for phrase in rule.get("strong_phrases", []):
            if phrase and phrase in meta_text:
                strong_meta_hits.append(phrase)
        if strong_meta_hits:
            score += min(15, len(strong_meta_hits[:3]) * 5)
            matched_signals.append(f"strong_meta:{', '.join(strong_meta_hits[:3])}")

        strong_title_hits = []
        for phrase in rule.get("strong_phrases", []):
            if phrase and any(phrase in title for title in collection_titles):
                strong_title_hits.append(phrase)
        if strong_title_hits:
            score += min(21, len(strong_title_hits[:3]) * 7)
            matched_signals.append(f"strong_title:{', '.join(strong_title_hits[:3])}")

        strong_handle_hits = []
        for phrase in rule.get("strong_phrases_compact", []):
            if phrase and any(phrase in handle for handle in collection_handles):
                strong_handle_hits.append(phrase)
        if strong_handle_hits:
            score += min(12, len(strong_handle_hits[:3]) * 4)
            matched_signals.append(f"strong_handle:{', '.join(strong_handle_hits[:3])}")

        return {
            "category": category_name,
            "score": score,
            "matched_signals": matched_signals,
            "domain_hit_count": len(domain_hits),
            "meta_hit_count": len(meta_hits),
            "title_hit_count": len(title_hits),
            "handle_hit_count": len(handle_hits),
            "strong_meta_hit_count": len(strong_meta_hits),
            "strong_title_hit_count": len(strong_title_hits),
            "strong_handle_hit_count": len(strong_handle_hits),
        }

    def _apply_focus_adjustments(self, signals, score_item):
        category_name = score_item["category"]
        anchored = (
            score_item.get("domain_hit_count", 0) > 0
            or score_item.get("meta_hit_count", 0) > 0
            or score_item.get("strong_meta_hit_count", 0) > 0
        )
        strong_total = (
            score_item.get("strong_meta_hit_count", 0)
            + score_item.get("strong_title_hit_count", 0)
            + score_item.get("strong_handle_hit_count", 0)
        )
        collection_count = int(signals.get("collection_count") or 0)

        if category_name == "pet" and not anchored and collection_count >= 120 and strong_total < 3:
            score_item["score"] = max(0, score_item["score"] - 15)
            score_item["matched_signals"].append("focus_penalty:pet_mixed_store")
        elif category_name == "bag" and not anchored and collection_count >= 100 and strong_total < 2:
            score_item["score"] = max(0, score_item["score"] - 10)
            score_item["matched_signals"].append("focus_penalty:bag_mixed_store")
        elif category_name == "garden" and not anchored and collection_count >= 80 and strong_total < 2:
            score_item["score"] = max(0, score_item["score"] - 12)
            score_item["matched_signals"].append("focus_penalty:garden_mixed_store")
        elif category_name == "auto" and not anchored and collection_count >= 40 and strong_total < 2:
            score_item["score"] = max(0, score_item["score"] - 12)
            score_item["matched_signals"].append("focus_penalty:auto_mixed_store")

        return score_item

    def _resolve_domain_direct_category(self, signals):
        domain_compact = signals["domain_compact"]
        candidates = []

        for category_name, rule in self.rules.items():
            domain_hits = []
            for keyword in rule["domain_keywords_compact"]:
                if len(keyword) >= 4 and keyword in domain_compact:
                    domain_hits.append(keyword)
            if not domain_hits:
                continue

            unique_hits = sorted(set(domain_hits), key=lambda item: (-len(item), item))
            candidates.append(
                {
                    "category": category_name,
                    "hits": unique_hits,
                    "longest_len": len(unique_hits[0]),
                    "hit_count": len(unique_hits),
                    "total_len": sum(len(item) for item in unique_hits),
                }
            )

        if not candidates:
            return None

        candidates.sort(
            key=lambda item: (
                item["longest_len"],
                item["hit_count"],
                item["total_len"],
            ),
            reverse=True,
        )
        best = candidates[0]
        if len(candidates) > 1:
            second = candidates[1]
            best_key = (best["longest_len"], best["hit_count"], best["total_len"])
            second_key = (second["longest_len"], second["hit_count"], second["total_len"])
            if best_key == second_key:
                return None

        return {
            "category": best["category"],
            "matched_signals": [f"domain_direct:{', '.join(best['hits'][:3])}"],
            "score": 100,
        }

    def classify_url(self, url):
        signals = self._build_signals(url)
        if not signals:
            return {
                "url": url,
                "base_url": "",
                "is_shopify": False,
                "status": "invalid_url",
            }

        if signals.get("has_arabic_content"):
            return {
                "url": url,
                "base_url": signals["base_url"],
                "is_shopify": signals["is_shopify"],
                "status": "arabic_content",
            }

        if not signals["is_shopify"]:
            if signals.get("had_request_error"):
                return {
                    "url": url,
                    "base_url": signals["base_url"],
                    "is_shopify": False,
                    "status": "fetch_failed",
                }
            return {
                "url": url,
                "base_url": signals["base_url"],
                "is_shopify": False,
                "status": "not_shopify",
            }

        domain_direct = self._resolve_domain_direct_category(signals)
        if domain_direct:
            return {
                "url": url,
                "base_url": signals["base_url"],
                "is_shopify": True,
                "status": "classified",
                "best_category": domain_direct["category"],
                "best_score": domain_direct["score"],
                "second_category": "",
                "second_score": 0,
                "matched_signals": domain_direct["matched_signals"],
                "scores": [],
                "collection_count": signals["collection_count"],
            }

        scores = []
        for category_name, rule in self.rules.items():
            score_item = self._score_category(signals, category_name, rule)
            score_item = self._apply_focus_adjustments(signals, score_item)
            if score_item["score"] > 0:
                scores.append(score_item)

        if not scores:
            return {
                "url": url,
                "base_url": signals["base_url"],
                "is_shopify": True,
                "status": "no_match",
                "scores": [],
            }

        scores.sort(key=lambda item: item["score"], reverse=True)
        best = scores[0]
        second = scores[1] if len(scores) > 1 else {"category": "", "score": 0}
        margin = int(best["score"]) - int(second["score"])
        confident = int(best["score"]) >= self.min_score and margin >= self.min_margin

        tied_categories = []
        if margin == 0 and len(scores) > 1:
            top_score = best["score"]
            tied = [s["category"] for s in scores if s["score"] == top_score and s["category"] in ("camera", "electronics")]
            if len(tied) > 1:
                tied_categories = tied

        if tied_categories:
            return {
                "url": url,
                "base_url": signals["base_url"],
                "is_shopify": True,
                "status": "classified",
                "best_category": tied_categories[0],
                "best_score": int(best["score"]),
                "second_category": tied_categories[1],
                "second_score": int(best["score"]),
                "tied_categories": tied_categories,
                "matched_signals": best["matched_signals"],
                "scores": scores[:5],
                "collection_count": signals["collection_count"],
            }

        return {
            "url": url,
            "base_url": signals["base_url"],
            "is_shopify": True,
            "status": "classified" if confident else "low_confidence",
            "best_category": best["category"],
            "best_score": int(best["score"]),
            "second_category": second["category"],
            "second_score": int(second["score"]),
            "matched_signals": best["matched_signals"],
            "scores": scores[:5],
            "collection_count": signals["collection_count"],
        }


def list_source_collections():
    client = MongoClient(MONGO_URI)
    try:
        db = client[MONGO_DB_NAME]
        names = []
        for name in sorted(db.list_collection_names()):
            if name.endswith("_Unfiltered_URLs"):
                names.append(name)
        return names
    finally:
        client.close()


def run_auto_classify_job(
    source_collection,
    limit=100,
    min_score=14,
    min_margin=6,
    delete_low_confidence=True,
    progress_callback=None,
    stop_callback=None,
):
    classifier = ShopifyAutoClassifier(
        min_score=min_score,
        min_margin=min_margin,
        progress_callback=progress_callback,
    )

    client = MongoClient(MONGO_URI)
    try:
        db = client[MONGO_DB_NAME]
        if source_collection not in db.list_collection_names():
            raise RuntimeError(f"未找到来源集合: {source_collection}")

        source = db[source_collection]
        docs = list(source.find({}, {"URL": 1}).limit(max(1, int(limit))))
        total = len(docs)
        if total == 0:
            return {
                "processed": 0,
                "classified": 0,
                "deleted": 0,
                "not_shopify": 0,
                "low_confidence": 0,
                "arabic_deleted": 0,
                "fetch_failed_deleted": 0,
                "failed": 0,
            }

        result = {
            "processed": 0,
            "classified": 0,
            "deleted": 0,
            "not_shopify": 0,
            "low_confidence": 0,
            "arabic_deleted": 0,
            "fetch_failed_deleted": 0,
            "failed": 0,
        }

        for index, doc in enumerate(docs, start=1):
            if stop_callback and stop_callback():
                return {**result, "stopped": True}

            doc_id = doc.get("_id")
            url = str(doc.get("URL") or "").strip()
            if progress_callback:
                progress_callback(f"[{index}/{total}] 正在分类: {url}")

            try:
                classified = classifier.classify_url(url)
                result["processed"] += 1

                if classified["status"] == "classified":
                    tied = classified.get("tied_categories")
                    if tied:
                        for cat in tied:
                            target_collection = f"{cat}_Filtered_URLs"
                            db[target_collection].update_one(
                                {"URL": classified["base_url"]},
                                {
                                    "$set": {
                                        "URL": classified["base_url"],
                                        "Category": cat,
                                        "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                        "AutoClassifiedFrom": source_collection,
                                        "AutoClassifyScore": classified["best_score"],
                                        "AutoClassifyMatchedSignals": classified["matched_signals"],
                                    }
                                },
                                upsert=True,
                            )
                        source.delete_one({"_id": doc_id})
                        result["classified"] += 1
                        if progress_callback:
                            progress_callback(
                                f"已分配到 {', '.join(tied)} (评分相同) | 得分={classified['best_score']} "
                                f"| 命中信号={'; '.join(classified['matched_signals'])}"
                            )
                        continue
                    else:
                        target_collection = f"{classified['best_category']}_Filtered_URLs"
                        db[target_collection].update_one(
                            {"URL": classified["base_url"]},
                            {
                                "$set": {
                                    "URL": classified["base_url"],
                                    "Category": classified["best_category"],
                                    "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    "AutoClassifiedFrom": source_collection,
                                    "AutoClassifyScore": classified["best_score"],
                                    "AutoClassifyMatchedSignals": classified["matched_signals"],
                                }
                            },
                            upsert=True,
                        )
                        source.delete_one({"_id": doc_id})
                        result["classified"] += 1
                        if progress_callback:
                            progress_callback(
                                f"已移动到 {target_collection} | 得分={classified['best_score']} "
                                f"| 命中信号={'; '.join(classified['matched_signals'])}"
                            )
                        continue

                source.delete_one({"_id": doc_id})
                result["deleted"] += 1

                if classified["status"] == "not_shopify":
                    result["not_shopify"] += 1
                    if progress_callback:
                        progress_callback("已删除来源 URL：不是 Shopify 站点")
                elif classified["status"] == "fetch_failed":
                    result["fetch_failed_deleted"] += 1
                    if progress_callback:
                        progress_callback("已删除来源 URL：站点打不开或接口抓取失败")
                elif classified["status"] == "arabic_content":
                    result["arabic_deleted"] += 1
                    if progress_callback:
                        progress_callback("已删除来源 URL：检测到阿拉伯文站点")
                elif classified["status"] == "low_confidence":
                    result["low_confidence"] += 1
                    if progress_callback:
                        progress_callback(
                            "已删除来源 URL：置信度不足 "
                            f"({classified.get('best_category', '')} {classified.get('best_score', 0)} 比 "
                            f"{classified.get('second_category', '')} {classified.get('second_score', 0)})"
                        )
                else:
                    if progress_callback:
                        progress_callback("已删除来源 URL：未命中任何分类")
            except Exception as exc:
                result["failed"] += 1
                if progress_callback:
                    progress_callback(f"分类失败 {url}: {exc}")

        return result
    finally:
        client.close()
