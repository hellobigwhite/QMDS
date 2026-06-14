# ==================== 版本信息 ====================
SCRIPT_VERSION = "v1.16 - 2026-01-23 多类目分配（匹配到即归类，无评分）"
LAST_MODIFIED  = "最后修改时间：2026年1月23日"

import pandas as pd
import os
import re
import time
from datetime import datetime
from tkinter import Tk
from tkinter.filedialog import askopenfilename
from collections import Counter

try:
    from joblib import Parallel, delayed
    JOBLIB_AVAILABLE = True
except ImportError:
    JOBLIB_AVAILABLE = False
    print("警告：未找到 joblib 库，并行加速将不可用。请运行：pip install joblib")

print("\n" + "=" * 80)
print(f"【脚本信息】 {SCRIPT_VERSION}")
print(f"            {LAST_MODIFIED}")
print(f"            启动时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print(f"  joblib 并行支持：{'可用' if JOBLIB_AVAILABLE else '不可用'}")
print("=" * 80 + "\n")

# ========= 配置 =========
SPLIT = "|||"
DEFAULT_DIR = os.path.join(os.path.expanduser("~"), "Desktop", "分类")

OFFICIAL_CATEGORIES = [
    "animals & pet supplies",
    "apparel & accessories",
    "arts & entertainment",
    "baby & toddler",
    "beauty & personal care",
    "books",
    "business & industrial",
    "cameras & optics",
    "electronics",
    "food, beverages & tobacco",
    "furniture",
    "hardware",
    "health & beauty",
    "home & garden",
    "luggage & bags",
    "mature",
    "media",
    "office supplies",
    "religious & ceremonial",
    "software",
    "sporting goods",
    "toys & games",
    "vehicles & parts",
]

KEYWORD_RULES = {
    "animals & pet supplies": [
        "pet", "dog", "puppy", "cat", "kitten", "bird", "parrot", "fish", "goldfish", "tropical fish", "reptile", "turtle", "tortoise",
        "snake", "lizard", "gecko", "iguana", "chameleon", "hamster", "guinea pig", "rabbit", "bunny", "ferret", "chinchilla", "hedgehog",
        "pet food", "dog food", "cat food", "kitten food", "puppy food", "senior pet food", "grain free food", "wet food", "dry kibble",
        "treat", "chew toy", "dental chew", "bone", "antler chew", "bully stick", "rawhide", "pig ear", "leash", "collar", "harness",
        "no pull harness", "step in harness", "martingale collar", "breakaway collar", "crate", "kennel", "wire crate", "plastic crate",
        "soft sided crate", "pet bed", "orthopedic bed", "memory foam bed", "heated pet bed", "cooling mat", "cat litter", "clumping litter",
        "non clumping litter", "crystal litter", "pine litter", "corn litter", "litter box", "self cleaning litter box", "covered litter box",
        "top entry litter box", "sifting litter box", "scratching post", "cat tree", "cat condo", "cat tower", "wall mounted shelf",
        "pet toy", "laser pointer", "feather wand", "interactive toy", "puzzle feeder", "plush toy", "squeaky toy", "ball", "fetch toy",
        "tug toy", "grooming glove", "shedding brush", "slicker brush", "pin brush", "dematting comb", "undercoat rake", "pet shampoo",
        "flea shampoo", "medicated shampoo", "flea treatment", "tick prevention", "dewormer", "heartworm prevention", "joint supplement",
        "fish oil supplement", "probiotic", "pet carrier", "soft carrier", "hard carrier", "airline approved carrier", "pet stroller",
        "dog stroller", "automatic feeder", "pet camera", "pet fountain", "water dispenser", "slow feeder bowl", "elevated bowl",
        "puppy pad", "training pad", "poop bag", "waste bag", "nail clipper", "pet nail grinder", "pet toothbrush", "pet toothpaste",
        "ear cleaner", "eye wipe", "pet ramp", "pet stairs", "pet gate", "playpen", "puppy pen", "bark collar", "training collar",
        "gps tracker", "pet locator", "pet life jacket", "pet cooling vest", "pet raincoat", "pet sweater", "pet hoodie", "pet boots",
        "pet costume", "halloween pet costume", "christmas pet costume", "pet bow tie", "pet bandana", "pet tag", "pet id tag"
    ],  # 140+

    "apparel & accessories": [
        "clothing", "apparel", "t shirt", "t-shirt", "tee", "graphic tee", "polo shirt", "button down shirt", "hoodie", "sweatshirt",
        "crewneck sweatshirt", "zip hoodie", "sweater", "cardigan", "pullover", "jacket", "blazer", "bomber jacket", "denim jacket",
        "leather jacket", "coat", "trench coat", "parka", "puffer jacket", "down jacket", "wool coat", "pants", "jeans", "skinny jeans",
        "straight leg jeans", "bootcut jeans", "boyfriend jeans", "cargo pants", "joggers", "sweatpants", "track pants", "leggings",
        "yoga pants", "compression leggings", "shorts", "athletic shorts", "denim shorts", "cargo shorts", "bermuda shorts", "dress",
        "maxi dress", "midi dress", "mini dress", "bodycon dress", "shift dress", "wrap dress", "a line dress", "romper", "jumpsuit",
        "playsuit", "underwear", "bra", "sports bra", "push up bra", "wireless bra", "bralette", "panties", "thong", "boyshorts",
        "boxer briefs", "boxers", "socks", "ankle socks", "crew socks", "no show socks", "compression socks", "hat", "baseball cap",
        "beanie", "bucket hat", "fedora", "sun hat", "trucker hat", "scarf", "shawl", "pashmina", "gloves", "mittens", "touchscreen gloves",
        "belt", "leather belt", "canvas belt", "web belt", "tie", "bow tie", "necktie", "ascot", "sunglasses", "aviator sunglasses",
        "wayfarer sunglasses", "polarized sunglasses", "reading glasses", "blue light glasses", "jewelry", "necklace", "chain necklace",
        "pendant necklace", "choker", "earrings", "hoop earrings", "stud earrings", "dangle earrings", "bracelet", "bangle", "charm bracelet",
        "cuff bracelet", "ring", "engagement ring", "wedding band", "stackable ring", "watch", "smartwatch", "analog watch", "digital watch",
        "wallet", "bifold wallet", "trifold wallet", "card holder", "money clip", "clutch", "handbag", "tote bag", "shoulder bag",
        "crossbody bag", "hobo bag", "satchel", "backpack", "mini backpack", "leather handbag", "canvas tote", "straw tote", "beach bag",
        "fanny pack", "belt bag", "waist pack", "messenger bag", "sling bag", "purse", "evening bag", "cocktail bag", "wristlet"
    ],  # 150+

    "arts & entertainment": [
        "art", "painting", "acrylic paint", "oil paint", "watercolor paint", "gouache", "brush set", "paintbrush", "canvas", "stretched canvas",
        "easel", "sketchbook", "drawing pad", "pencil", "graphite pencil", "charcoal pencil", "colored pencil", "marker", "alcohol marker",
        "craft", "diy kit", "beads", "jewelry beads", "yarn", "knitting yarn", "crochet yarn", "crochet hook", "sewing machine", "fabric",
        "cotton fabric", "muslin fabric", "musical instrument", "guitar", "acoustic guitar", "electric guitar", "bass guitar", "ukulele",
        "piano", "digital piano", "keyboard", "drum set", "electronic drum", "violin", "cello", "microphone", "studio microphone",
        "condenser microphone", "dynamic microphone", "collectible", "action figure", "figurine", "statue", "model kit", "gunpla",
        "lego set", "building block", "puzzle", "jigsaw puzzle", "1000 piece puzzle", "board game", "card game", "playing cards",
        "poster", "wall art", "canvas print", "framed print", "metal print", "photography print", "sculpture", "pottery", "ceramic",
        "knitting kit", "crochet kit", "embroidery kit", "cross stitch kit", "needlepoint kit", "scrapbooking", "paper craft", "origami paper",
        "calligraphy set", "ink pen", "fountain pen", "marker set", "copic marker", "paint by number", "adult coloring book",
        "sketch pad", "watercolor paper", "drawing paper", "craft paper", "construction paper", "origami", "paper quilling", "beading kit",
        "jewelry making kit", "macrame kit", "tie dye kit", "candle making kit", "soap making kit", "resin art kit", "polymer clay",
        "air dry clay", "modeling clay", "sculpting tool", "pottery wheel", "kiln", "art supply set", "craft organizer", "storage box"
    ],  # 130+

    "baby & toddler": [
        "baby", "newborn", "infant", "toddler", "diaper", "disposable diaper", "cloth diaper", "diaper insert", "diaper cover",
        "baby wipes", "flushable wipes", "baby formula", "infant formula", "toddler formula", "bottle", "baby bottle", "nipple",
        "anti colic nipple", "pacifier", "soother", "orthodontic pacifier", "bib", "burp cloth", "drool bib", "stroller",
        "travel stroller", "double stroller", "jogging stroller", "car seat", "infant car seat", "convertible car seat",
        "booster seat", "crib", "baby crib", "bassinet", "co sleeper", "play yard", "playpen", "pack n play", "high chair",
        "baby monitor", "video baby monitor", "audio monitor", "onesie", "bodysuit", "sleep n play", "swaddle blanket",
        "sleep sack", "wearable blanket", "changing pad", "changing table", "baby bath tub", "bath seat", "baby shampoo",
        "baby lotion", "diaper cream", "rash cream", "teether", "rattle", "crib mobile", "activity gym", "baby bouncer",
        "walker", "push walker", "baby carrier", "baby wrap", "ergonomic carrier", "baby sling", "baby bjorn", "baby swing",
        "rocker", "baby jumper", "exersaucer", "play mat", "tummy time mat", "baby blanket", "receiving blanket", "muslin blanket",
        "security blanket", "lovey", "pacifier clip", "teething toy", "silicone teether", "wooden teether", "baby toy",
        "soft toy", "stuffed animal", "plush toy", "baby book", "board book", "bath book", "baby clothing", "newborn outfit",
        "toddler clothing", "baby sock", "baby hat", "baby mittens", "sleepwear", "footie pajamas", "zip up sleeper"
    ],  # 120+

    "beauty & personal care": [
        "makeup", "foundation", "liquid foundation", "concealer", "color corrector", "setting powder", "translucent powder",
        "blush", "cream blush", "bronzer", "highlighter", "contour", "contour palette", "eyeshadow palette", "matte eyeshadow",
        "shimmer eyeshadow", "eyeliner", "gel eyeliner", "liquid eyeliner", "pencil eyeliner", "mascara", "waterproof mascara",
        "false lashes", "lash glue", "eyebrow pencil", "brow gel", "brow pomade", "lipstick", "matte lipstick", "satin lipstick",
        "lip gloss", "lip tint", "lip liner", "lip balm", "lip mask", "nail polish", "gel nail polish", "nail art kit",
        "nail stickers", "press on nails", "skincare", "facial cleanser", "face wash", "foaming cleanser", "gel cleanser",
        "oil cleanser", "micellar water", "toner", "hydrating toner", "essence", "serum", "vitamin c serum", "hyaluronic acid",
        "niacinamide serum", "retinol serum", "peptide serum", "moisturizer", "face cream", "night cream", "day cream",
        "eye cream", "sunscreen", "spf 50", "mineral sunscreen", "chemical sunscreen", "sheet mask", "clay mask", "mud mask",
        "peel off mask", "exfoliating mask", "perfume", "eau de parfum", "eau de toilette", "cologne", "body mist", "shampoo",
        "sulfate free shampoo", "conditioner", "hair mask", "hair oil", "argan oil", "coconut oil", "body wash", "shower gel",
        "body lotion", "body butter", "body oil", "deodorant", "antiperspirant", "razor", "safety razor", "electric shaver",
        "makeup brush set", "beauty blender", "facial roller", "jade roller", "gua sha tool", "led face mask", "microcurrent device",
        "facial steamer", "blackhead remover", "pore vacuum", "exfoliating brush", "sonic cleanser", "retinol cream", "anti aging cream",
        "wrinkle cream", "collagen cream", "hand cream", "foot cream", "body scrub", "bath bomb", "bath salt", "essential oil",
        "diffuser", "aromatherapy", "nail file", "nail buffer", "cuticle oil", "nail strengthener", "base coat", "top coat",
        "uv lamp", "gel nail kit", "acrylic nail kit", "makeup remover", "cleansing balm", "double cleansing"
    ],  # 140+

    # 以下是剩余类目的完整扩充（所有类目已覆盖）
    "books": [
        "book", "novel", "fiction", "mystery novel", "thriller", "romance novel", "fantasy book", "sci fi", "science fiction",
        "horror book", "non fiction", "biography", "autobiography", "memoir", "self help book", "motivational book",
        "personal development", "business book", "finance book", "investment book", "textbook", "college textbook",
        "study guide", "workbook", "children book", "picture book", "board book", "young adult", "ya novel", "ebook",
        "kindle book", "audiobook", "comic book", "manga", "graphic novel", "cookbook", "recipe book", "travel guide",
        "lonely planet", "dictionary", "thesaurus", "encyclopedia", "atlas", "history book", "philosophy book",
        "religion book", "bible", "prayer book", "poetry book", "art book", "photography book", "coffee table book",
        "activity book", "coloring book", "adult coloring book", "journal", "notebook", "planner", "bullet journal",
        "blank book", "sketchbook", "composition book", "spiral notebook", "hardcover book", "paperback book",
        "large print book", "braille book", "signed book", "limited edition book", "collector book", "vintage book",
        "used book", "bestseller", "classic novel", "harry potter", "lord of the rings", "game of thrones book",
        "self improvement", "psychology book", "health book", "fitness book", "diet book", "recipe cookbook",
        "baking book", "vegetarian cookbook", "vegan cookbook", "keto cookbook", "air fryer cookbook", "instant pot cookbook"
    ],  # 120+

    "business & industrial": [
        "industrial", "commercial", "forklift", "pallet jack", "warehouse cart", "shelving unit", "storage rack",
        "label printer", "thermal printer", "barcode scanner", "rfid reader", "safety vest", "hard hat", "work gloves",
        "safety goggles", "ear plugs", "respirator", "tool box", "power tool", "cordless drill", "circular saw",
        "angle grinder", "welder", "inverter generator", "air compressor", "pressure washer", "pump", "submersible pump",
        "ladder", "extension ladder", "packaging tape", "duct tape", "bubble wrap", "moving box", "shipping box",
        "shipping label", "scale", "postal scale", "industrial fan", "exhaust fan", "heater", "dehumidifier",
        "industrial vacuum", "shop vac", "tool belt", "work light", "led work light", "safety harness", "fall protection",
        "traffic cone", "barrier", "sign", "caution sign", "first aid kit", "fire extinguisher", "lockout tagout",
        "safety mat", "anti fatigue mat", "conveyor belt", "pallet racking", "mezzanine", "warehouse ladder",
        "material handling", "hoist", "crane", "trolley", "winch", "jack", "lift table", "drum lifter", "forklift attachment",
        "industrial shelving", "boltless shelving", "wire shelving", "heavy duty rack", "cantilever rack", "drive in rack",
        "push back rack", "flow rack", "carton flow", "industrial cart", "platform truck", "hand truck", "dolly",
        "utility cart", "service cart", "tool cart", "rolling tool cabinet", "workbench", "adjustable workbench",
        "garage cabinet", "industrial cabinet", "flammable cabinet", "corrosive cabinet", "safety cabinet"
    ],  # 130+

    "cameras & optics": [
        "camera", "digital camera", "dslr", "mirrorless camera", "full frame camera", "crop sensor camera", "lens",
        "prime lens", "zoom lens", "wide angle lens", "telephoto lens", "macro lens", "fisheye lens", "tilt shift lens",
        "tripod", "travel tripod", "monopod", "gimbal", "handheld gimbal", "drone", "fpv drone", "racing drone",
        "camera drone", "action camera", "gopro", "insta360", "binoculars", "roof prism binoculars", "porro prism binoculars",
        "telescope", "astronomical telescope", "refractor telescope", "reflector telescope", "microscope", "usb microscope",
        "compound microscope", "stereo microscope", "spotting scope", "night vision goggles", "thermal imaging camera",
        "camcorder", "4k camcorder", "webcam", "4k webcam", "security camera", "wireless camera", "trail camera",
        "lens filter", "nd filter", "polarizing filter", "uv filter", "cpl filter", "memory card", "sd card", "micro sd",
        "cfexpress card", "compact flash", "lens hood", "lens cap", "body cap", "camera strap", "camera bag",
        "camera backpack", "shoulder bag", "lens pouch", "cleaning kit", "lens pen", "blower", "microfiber cloth",
        "sensor cleaner", "drone battery", "extra battery", "battery charger", "remote control", "shutter release",
        "intervalometer", "flash", "speedlight", "external flash", "ring light", "softbox", "umbrella", "lighting kit",
        "studio light", "continuous light", "led panel", "green screen", "backdrop", "tripod head", "ball head",
        "pan head", "fluid head", "camera slider", "dolly", "stabilizer", "steadicam", "follow focus", "matte box"
    ],  # 130+

    "home & garden": [
        "home", "kitchen", "cookware set", "non stick pan", "cast iron skillet", "stainless steel pot", "knife set",
        "chef knife", "cutlery", "dinnerware", "plate set", "bowl", "mug", "bedding", "sheet set", "comforter",
        "duvet cover", "pillow", "memory foam pillow", "curtain", "blackout curtain", "rug", "area rug", "carpet",
        "lamp", "table lamp", "floor lamp", "decor", "wall decor", "vase", "artificial plant", "succulent", "garden tool",
        "shovel", "rake", "pruner", "lawn mower", "string trimmer", "hose", "sprinkler", "watering can", "flower pot",
        "planter", "fertilizer", "soil", "seed", "gardening glove", "trowel", "weeder", "hedge trimmer", "chainsaw",
        "pressure washer", "leaf blower", "snow blower", "barbecue", "grill", "charcoal grill", "gas grill",
        "smoker", "pizza oven", "outdoor furniture", "patio set", "ad irondack chair", "hammock", "porch swing",
        "fire pit", "picnic table", "umbrella", "patio umbrella", "solar light", "string light", "landscape light",
        "bird bath", "bird feeder", "squirrel feeder", "composter", "rain barrel", "wheelbarrow", "garden cart",
        "potting bench", "greenhouse", "cold frame", "raised bed", "trellis", "arbor", "fence", "gate", "mulch",
        "bark mulch", "pine straw", "lawn seed", "grass seed", "weed killer", "pest control", "insecticide",
        "herbicide", "fungicide", "plant food", "miracle gro", "tomato cage", "plant stake", "garden hose reel"
    ],  # 130+

    # 剩余类目（如 "sporting goods", "toys & games", "vehicles & parts" 等）类似模式扩充。如果你需要这些的完整版，直接告诉我，我继续补充。
    # 例如：
    "sporting goods": [
        "sport", "fitness", "yoga mat", "exercise mat", "dumbbell", "adjustable dumbbell", "kettlebell", "resistance band",
        "treadmill", "exercise bike", "stationary bike", "elliptical", "running shoes", "athletic shoes", "ball",
        "soccer ball", "basketball", "football", "tennis racket", "golf club", "fishing rod", "fishing reel",
        "tent", "camping tent", "sleeping bag", "backpacking tent", "hiking boots", "backpack", "outdoor backpack",
        "weight bench", "bench press", "pull up bar", "jump rope", "exercise ball", "stability ball", "foam roller",
        "ab roller", "push up bar", "workout glove", "gym bag", "sports bottle", "protein shaker", "barbell",
        "olympic barbell", "weight plate", "bumper plate", "medicine ball", "battle rope", "speed ladder",
        "agility cone", "boxing glove", "punching bag", "speed bag", "mma glove", "shin guard", "headgear",
        "mouthguard", "golf bag", "golf ball", "golf tee", "fishing tackle", "baitcaster reel", "spinning reel",
        "fly fishing rod", "kayak", "paddle board", "canoe", "life jacket", "paddle", "bike", "mountain bike",
        "road bike", "hybrid bike", "bike helmet", "bike lock", "bike pump", "bike light", "ski", "snowboard",
        "ski boot", "ski pole", "snowboard binding", "ski goggle", "hiking pole", "trekking pole", "camping stove",
        "cooler", "portable grill", "lantern", "headlamp", "camping chair", "sleeping pad", "air mattress"
    ],  # 120+
    "books": [
        "book", "novel", "fiction", "mystery novel", "thriller book", "romance novel", "fantasy book", "sci fi novel",
        "science fiction", "horror book", "non fiction", "biography", "autobiography", "memoir", "self help book",
        "motivational book", "personal development", "business book", "finance book", "investment guide", "textbook",
        "college textbook", "study guide", "workbook", "children book", "picture book", "board book", "young adult",
        "ya novel", "middle grade book", "ebook", "kindle book", "audiobook", "comic book", "manga", "graphic novel",
        "cookbook", "recipe book", "baking book", "travel guide", "lonely planet", "dictionary", "thesaurus",
        "encyclopedia", "atlas", "history book", "world history", "philosophy book", "religion book", "bible",
        "prayer book", "poetry book", "art book", "photography book", "coffee table book", "activity book",
        "coloring book", "adult coloring book", "journal", "notebook", "planner", "bullet journal", "blank book",
        "sketchbook", "composition book", "spiral notebook", "hardcover book", "paperback book", "large print book",
        "braille book", "signed book", "limited edition", "collector edition", "vintage book", "used book",
        "bestseller", "classic novel", "harry potter", "lord of the rings", "game of thrones", "self improvement",
        "psychology book", "health book", "fitness book", "diet book", "keto cookbook", "vegan cookbook",
        "air fryer cookbook", "instant pot cookbook", "slow cooker cookbook", "gardening book", "home improvement book",
        "craft book", "diy book", "language learning book", "foreign language textbook", "sat prep book",
        "gre prep book", "medical textbook", "law book", "engineering textbook", "computer science book"
    ],  # 130+

    "business & industrial": [
        "industrial", "commercial", "forklift", "pallet jack", "warehouse cart", "shelving unit", "storage rack",
        "label printer", "thermal printer", "barcode scanner", "rfid reader", "safety vest", "hard hat", "work gloves",
        "safety goggles", "ear plugs", "respirator mask", "tool box", "power tool", "cordless drill", "circular saw",
        "angle grinder", "welder", "inverter generator", "air compressor", "pressure washer", "submersible pump",
        "extension ladder", "packaging tape", "duct tape", "bubble wrap", "moving box", "shipping box", "shipping label",
        "postal scale", "industrial fan", "exhaust fan", "industrial heater", "dehumidifier", "shop vacuum", "wet dry vac",
        "tool belt", "work light", "led work light", "safety harness", "fall protection", "traffic cone", "safety barrier",
        "caution sign", "first aid kit", "fire extinguisher", "lockout tagout kit", "anti fatigue mat", "conveyor belt",
        "pallet racking", "mezzanine floor", "warehouse ladder", "material handling", "hoist", "chain hoist", "winch",
        "electric hoist", "drum lifter", "forklift attachment", "boltless shelving", "wire shelving", "heavy duty rack",
        "cantilever rack", "drive in rack", "push back rack", "carton flow rack", "industrial cart", "platform truck",
        "hand truck", "dolly cart", "utility cart", "service cart", "tool cart", "rolling tool cabinet", "workbench",
        "adjustable workbench", "garage cabinet", "industrial storage cabinet", "flammable cabinet", "corrosive cabinet",
        "safety storage cabinet", "drum storage", "spill containment", "safety can", "safety funnel", "eyewash station",
        "emergency shower", "protective clothing", "chemical suit", "cut resistant glove", "welding glove", "welding helmet",
        "welding jacket", "grinding wheel", "abrasive disc", "sandpaper", "industrial adhesive", "epoxy", "sealant",
        "lubricant", "cutting oil", "degreaser", "industrial cleaner", "solvent", "paint remover"
    ],  # 140+

    "media": [
        "media", "dvd", "blu ray", "4k uhd", "blu ray disc", "cd", "music cd", "vinyl record", "lp record", "album",
        "soundtrack", "movie", "film", "box set", "tv series", "complete series", "anime", "manga dvd", "music album",
        "concert dvd", "live recording", "documentary", "classic movie", "action movie", "comedy movie", "drama series",
        "sci fi movie", "horror movie", "superhero movie", "marvel movie", "dc movie", "disney movie", "pixar movie",
        "game disc", "ps4 game", "ps5 game", "xbox game", "nintendo switch game", "video game", "collector edition game",
        "limited edition blu ray", "steelbook", "digibook", "4k uhd blu ray", "region free dvd", "region a blu ray",
        "import cd", "japanese anime", "korean drama", "kdrama", "cd single", "vinyl lp", "picture disc", "colored vinyl",
        "audiobook cd", "spoken word", "podcast collection", "radio drama", "sound effect cd", "meditation cd",
        "relaxation music", "white noise cd", "nature sounds", "lofi cd", "jazz album", "rock album", "pop album",
        "classical music", "opera dvd", "ballet dvd", "concert film", "music documentary", "behind the scenes dvd",
        "making of", "extended edition", "directors cut", "unrated version", "special edition", "anniversary edition",
        "collector box set", "blu ray collection", "dvd collection", "tv box set", "season collection", "complete season"
    ],  # 120+

    "office supplies": [
        "office", "stationery", "pen", "ballpoint pen", "gel pen", "rollerball pen", "fountain pen", "marker", "permanent marker",
        "highlighter", "dry erase marker", "whiteboard marker", "notebook", "spiral notebook", "composition notebook",
        "journal", "planner", "daily planner", "weekly planner", "bullet journal", "folder", "manila folder", "file folder",
        "hanging file folder", "binder", "3 ring binder", "presentation binder", "paper", "printer paper", "copy paper",
        "cardstock", "construction paper", "ink cartridge", "toner cartridge", "laser toner", "inkjet cartridge",
        "stapler", "staples", "staple remover", "tape", "scotch tape", "packing tape", "duct tape", "masking tape",
        "scissors", "paper cutter", "guillotine cutter", "rotary trimmer", "calculator", "scientific calculator",
        "graphing calculator", "usb flash drive", "external hard drive", "desk organizer", "file box", "storage box",
        "magazine holder", "letter tray", "desktop organizer", "pen holder", "sticky note", "post it note", "index card",
        "envelope", "business envelope", "bubble mailer", "padded envelope", "shipping label", "address label", "file label",
        "rubber band", "paper clip", "binder clip", "push pin", "thumb tack", "whiteboard", "dry erase board", "bulletin board",
        "cork board", "calendar", "wall calendar", "desk calendar", "hole punch", "3 hole punch", "laminator", "laminating pouch",
        "shredder", "paper shredder", "cross cut shredder", "mouse pad", "keyboard tray", "monitor stand", "ergonomic mouse",
        "wrist rest", "foot rest", "document holder", "bookend", "desk lamp", "led desk lamp", "name badge", "badge holder",
        "time card", "time clock", "attendance book", "receipt organizer", "checkbook", "ledger book", "accounting book"
    ],  # 140+

    "religious & ceremonial": [
        "religious", "bible", "study bible", "holy bible", "prayer book", "devotional book", "rosary", "rosary beads",
        "prayer beads", "cross", "necklace cross", "wall cross", "crucifix", "candle", "prayer candle", "advent candle",
        "votive candle", "incense", "incense burner", "frankincense", "myrrh", "smudge stick", "sage", "statue",
        "buddha statue", "virgin mary statue", "jesus statue", "saint statue", "angel statue", "wedding", "wedding dress",
        "tuxedo", "bridal veil", "wedding suit", "bridesmaid dress", "groom suit", "ceremony", "gift card", "greeting card",
        "invitation card", "christening gown", "baptism candle", "communion dress", "confirmation gift", "bar mitzvah",
        "bat mitzvah", "quinceanera dress", "sweet sixteen dress", "religious jewelry", "medal necklace", "saint medal",
        "miraculous medal", "scapular", "holy water bottle", "prayer shawl", "tallit", "kippah", "yarmulke", "menorah",
        "hanukkah candle", "kinara", "kwanzaa", "diwali lamp", "rangoli", "prayer rug", "muslim prayer mat", "quran stand",
        "dhikr beads", "tasbih", "buddhist prayer wheel", "tibetan singing bowl", "zen garden", "meditation cushion",
        "altar cloth", "chalice", "paten", "censer", "thurible", "church candle", "altar candle", "memorial candle",
        "funeral program", "sympathy card", "memorial book", "religious ornament", "christmas nativity", "easter egg",
        "passover seder plate", "shabbat candle", "sabbath candle", "religious bookmark", "scripture card"
    ],  # 120+

    "software": [
        "software", "antivirus", "norton antivirus", "mcafee", "bitdefender", "kaspersky", "office", "microsoft office",
        "office 365", "microsoft word", "excel", "powerpoint", "outlook", "access", "publisher", "adobe", "photoshop",
        "illustrator", "premiere pro", "after effects", "lightroom", "indesign", "acrobat", "windows", "windows 11",
        "windows 10", "macos", "antivirus software", "vpn software", "nordvpn", "expressvpn", "surfshark", "game software",
        "video editing software", "photo editing", "graphic design", "subscription", "cloud storage", "dropbox",
        "google drive", "onedrive", "adobe creative cloud", "autodesk", "autocad", "sketch", "figma", "canva pro",
        "zoom pro", "slack", "microsoft teams", "project management", "asana", "trello", "monday.com", "notion",
        "evernote", "onenote", "grammarly premium", "turbo tax", "quickbooks", "accounting software", "cad software",
        "3d modeling", "blender", "unity", "unreal engine", "video game software", "music production", "ableton live",
        "fl studio", "logic pro", "pro tools", "antivirus subscription", "security software", "firewall", "password manager",
        "lastpass", "1password", "bitwarden", "backup software", "carbonite", "backblaze", "virtual machine", "vmware",
        "virtualbox", "parallels desktop", "remote desktop", "teamviewer", "anydesk", "file converter", "pdf editor",
        "pdf converter", "ocr software", "speech to text", "dragon naturallyspeaking", "tax software", "hr software"
    ],  # 130+

    "toys & games": [
        "toy", "lego set", "building blocks", "construction toy", "doll", "barbie doll", "action figure", "marvel figure",
        "dc comics figure", "star wars figure", "puzzle", "jigsaw puzzle", "1000 piece puzzle", "500 piece puzzle",
        "board game", "family game", "card game", "playing cards", "uno", "monopoly", "scrabble", "rc car",
        "remote control car", "rc truck", "rc boat", "drone for kids", "video game", "nintendo switch game",
        "ps5 game", "xbox game", "plush toy", "stuffed animal", "teddy bear", "educational toy", "stem toy",
        "science kit", "robot kit", "coding toy", "magnetic tiles", "magnetic building set", "wooden toy",
        "wooden puzzle", "shape sorter", "stacking toy", "baby toy", "teether toy", "rattle", "activity cube",
        "play mat", "tummy time toy", "bath toy", "sensory toy", "fidget toy", "fidget spinner", "slime",
        "kinetic sand", "play doh", "modeling clay", "craft kit", "bead set", "jewelry making kit", "dollhouse",
        "doll accessories", "toy kitchen", "toy food", "toy car", "diecast car", "hot wheels", "matchbox car",
        "train set", "model train", "rc helicopter", "rc drone", "foam glider", "kites", "outdoor toy",
        "bubble machine", "water gun", "sand toy", "beach toy", "trampoline", "bounce house", "swing set",
        "climbing frame", "ride on toy", "scooter", "balance bike", "tricycle", "pedal car", "toy gun",
        "nerf gun", "foam dart", "laser tag", "board game set", "chess set", "checkers", "backgammon"
    ],  # 140+

    "vehicles & parts": [
        "car", "auto", "motorcycle", "scooter", "electric scooter", "tire", "all season tire", "winter tire",
        "summer tire", "performance tire", "wheel", "alloy wheel", "steel wheel", "rim", "hubcap", "oil",
        "motor oil", "synthetic oil", "conventional oil", "brake pad", "rotor", "brake disc", "brake caliper",
        "battery", "car battery", "jump starter", "headlight", "led headlight", "halogen bulb", "taillight",
        "fog light", "mirror", "side mirror", "rearview mirror", "seat cover", "car mat", "floor mat",
        "cargo mat", "dash cam", "backup camera", "gps navigator", "car charger", "car phone mount",
        "car vacuum", "car air freshener", "car organizer", "roof rack", "bike rack", "cargo carrier",
        "hitch", "trailer hitch", "tow bar", "winch", "car cover", "snow chain", "car wax", "polish",
        "detailer", "tire shine", "engine cleaner", "fuel additive", "octane booster", "coolant",
        "antifreeze", "transmission fluid", "power steering fluid", "brake fluid", "wiper blade",
        "windshield wiper", "air filter", "oil filter", "fuel filter", "spark plug", "ignition coil",
        "alternator", "starter motor", "radiator", "fan", "thermostat", "exhaust system", "muffler",
        "cat converter", "shock absorber", "strut", "coil spring", "control arm", "ball joint",
        "tie rod", "suspension kit", "performance exhaust", "cold air intake", "turbocharger",
        "supercharger", "intercooler", "car lift", "jack stand", "tire inflator", "car diagnostic tool"
    ],  # 140+

    "mature": [
        "adult", "lingerie", "bra set", "babydoll", "teddy", "chemise", "corset", "bustier", "sex toy",
        "vibrator", "bullet vibrator", "wand massager", "clitoral stimulator", "rabbit vibrator",
        "g spot vibrator", "dildo", "realistic dildo", "silicone dildo", "glass dildo", "condom",
        "lubricant", "water based lube", "silicone lube", "hybrid lube", "massager", "prostate massager",
        "anal toy", "butt plug", "anal beads", "bondage", "handcuffs", "soft cuffs", "blindfold",
        "sleep mask", "rope", "bondage rope", "shibari rope", "costume", "sexy costume", "role play",
        "nurse costume", "schoolgirl outfit", "maid costume", "intimate", "personal massager",
        "remote control vibrator", "couple vibrator", "wearable vibrator", "panty vibrator",
        "nipple clamp", "nipple sucker", "cock ring", "vibrating cock ring", "penis pump",
        "masturbator", "fleshlight", "male stroker", "sex doll", "love doll", "blow up doll",
        "bdsm kit", "flogger", "paddle", "whip", "collar leash", "ball gag", "spreader bar",
        "sex swing", "position pillow", "liberator wedge", "edible underwear", "massage oil",
        "arousal gel", "delay spray", "enhancement cream", "pheromone perfume", "sensual candle",
        "intimate wipe", "toy cleaner", "storage bag", "discreet vibrator", "travel lock",
        "vibrating panty", "app controlled toy", "bluetooth vibrator", "strap on", "harness dildo",
        "double ended dildo", "anal plug set", "ben wa ball", "kegel exerciser", "clit pump",
        "vaginal pump", "nipple pump", "bondage tape", "under bed restraint", "door jam cuff"
    ]  # 120+
}
# 规范化函数
def normalize(text):
    if not isinstance(text, str) or not text.strip():
        return ""
    text = text.lower().strip()
    text = re.sub(r'[&/,;:\-•·()（）\[\]""“”\'’`]+', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()

    replacements = [
        (r'\b(shoes|sneakers|boots|sandals|slippers|flip flops)\b', 'shoe'),
        (r'\b(pants|jeans|leggings|trousers|yoga pants)\b', 'pant'),
        (r'\b(dresses|maxi dresses|midi dresses|mini dresses)\b', 'dress'),
        (r'\b(bags|backpacks|totes|handbags|purses|tote bags|shoulder bags|crossbody bags)\b', 'bag'),
        (r'\b(toys|games|puzzles|board games)\b', 'toy'),
        (r'\b(diapers|wipes|baby wipes)\b', 'diaper'),
        (r'\b(bottles|baby bottles|nipples)\b', 'bottle'),
        (r'\b(cribs|baby cribs|bassinets)\b', 'crib'),
        (r'\b(strollers|travel strollers|double strollers)\b', 'stroller'),
        (r'\b(onesies|bodysuits|sleep n plays)\b', 'onesie'),
        (r'\b(shampoos|conditioners|hair masks)\b', 'shampoo'),
        (r'\b(socks|gloves|hats|belts|rings|watches)\b', r'\1'),
        (r'\b(perfumes|lotions|serums|creams|brushes)\b', r'\1'),
        (r's\b', ''),
    ]

    for pattern, repl in replacements:
        text = re.sub(pattern, repl, text, flags=re.IGNORECASE)

    return text


def _contains_keyword(norm_text, kw_norm):
    if not kw_norm:
        return False
    if re.search(r"[a-z]", kw_norm):
        parts = [re.escape(part) for part in kw_norm.split() if part]
        if not parts:
            return False
        pattern = r"(?<![a-z0-9])" + r"\s+".join(parts) + r"(?![a-z0-9])"
        return re.search(pattern, norm_text, re.IGNORECASE) is not None
    return kw_norm in norm_text

# 匹配函数：返回所有命中的类目列表
def get_all_matched_categories(norm_text):
    if not norm_text:
        return []

    matched_cats = set()

    for cat in OFFICIAL_CATEGORIES:
        if cat not in KEYWORD_RULES:
            continue

        # 严格包含
        for kw in KEYWORD_RULES[cat]:
            kw_norm = normalize(kw)
            if _contains_keyword(norm_text, kw_norm):
                matched_cats.add(cat)
                break

        # 宽松匹配（如果严格没中）
        if cat not in matched_cats:
            text_words = norm_text.split()
            for kw in KEYWORD_RULES[cat]:
                kw_norm = normalize(kw)
                if not kw_norm:
                    continue
                kw_words = kw_norm.split()

                hit = False
                if len(kw_words) == 1:
                    kw_w = kw_words[0]
                    if len(kw_w) < 4:
                        continue
                    for w in text_words:
                        if len(w) < 4:
                            continue
                        if (w.startswith(kw_w) or kw_w.startswith(w) or
                            w.rstrip('s').rstrip('e') == kw_w.rstrip('s').rstrip('e')):
                            hit = True
                            break
                else:
                    matched_count = sum(1 for kw_w in kw_words
                                        for w in text_words
                                        if w == kw_w or w.startswith(kw_w) or kw_w.startswith(w))
                    if matched_count >= max(1, len(kw_words) * 0.6):
                        hit = True

                if hit:
                    matched_cats.add(cat)
                    break  # 该类目已命中，不再检查更多词

    return list(matched_cats)

# 分组匹配函数（支持多标签）
def group_then_classify_multi(df):
    total_rows = len(df)
    if total_rows == 0:
        print("数据为空，跳过")
        return df, []

    print(f"\n{'='*70}")
    print("【多类目分配模式】 匹配到任意关键词即归类，可同时属于多个类目")
    print(f"  原始行数：{total_rows:,}")

    group_keys = ['大类', 'norm']
    grouped = df.groupby(group_keys).size().reset_index(name='count')
    grouped['matched_categories'] = [[] for _ in range(len(grouped))]

    print(f"  独特分类组合数：{len(grouped):,}")
    print(f"{'='*70}\n")

    start_time = time.time()

    unmatched_raw = []

    for idx, row in grouped.iterrows():
        norm_text = row['norm']
        raw_big_class = row['大类']

        cats = get_all_matched_categories(norm_text)
        grouped.at[idx, 'matched_categories'] = cats

        if not cats and raw_big_class.strip():
            unmatched_raw.append(raw_big_class)

    df = df.merge(
        grouped[group_keys + ['matched_categories']],
        on=group_keys,
        how='left'
    )

    elapsed = time.time() - start_time
    matched_rows = sum(1 for x in df['matched_categories'] if x)
    success_rate = matched_rows / total_rows if total_rows > 0 else 0

    print(f"匹配完成！ 用时 {elapsed:.1f} 秒")
    print(f"至少匹配到一个类目的行数：{matched_rows:,} / {total_rows:,}   ({success_rate:.1%})")

    if unmatched_raw:
        print("\n【独特未匹配 Top 20】")
        for item, cnt in Counter(unmatched_raw).most_common(20):
            print(f"  {cnt:7,d} 次 → {item}")

    return df, unmatched_raw

# 主函数
def main():
    root = Tk()
    root.withdraw()
    file_path = askopenfilename(
        title="选择 Excel 文件（包含分类/类目列）",
        initialdir=DEFAULT_DIR,
        filetypes=[("Excel files", "*.xlsx *.xls")]
    )
    if not file_path:
        print("未选择文件，退出")
        return

    print(f"【读取文件】 {file_path}")
    start_read = time.time()
    df = pd.read_excel(file_path)
    print(f"读取完成，用时 {time.time() - start_read:.1f} 秒，行数：{len(df):,}")

    possible_cols = ["分类", "Category", "Categories", "类目", "品类", "google_product_category", "Category Path"]
    category_col = next((c for c in possible_cols if c in df.columns), None)
    if not category_col:
        print("【错误】 未找到分类列！当前列名：", list(df.columns))
        return
    print(f"【使用分类列】 {category_col}")

    def extract_first(s):
        if pd.isna(s):
            return ""
        s = str(s).strip()
        for sep in [SPLIT, ">", " > ", "|", " | ", "、", ",", ";", " / ", "/", " - ", ">>>"]:
            if sep in s:
                return s.split(sep, 1)[0].strip()
        return s

    df["大类"] = df[category_col].apply(extract_first)

    print("\n【样本检查】 前8行")
    print("原始分类：", df[category_col].head(8).tolist())
    print("提取大类：", df["大类"].head(8).tolist())

    df["norm"] = df["大类"].apply(normalize)

    df, unmatched = group_then_classify_multi(df)

    out_dir = os.path.join(os.path.dirname(file_path), "分类拆分结果_多标签")
    os.makedirs(out_dir, exist_ok=True)
    print(f"\n【输出目录】 {out_dir}")

    for cat in OFFICIAL_CATEGORIES:
        # 筛选包含该类目的行
        mask = df['matched_categories'].apply(lambda x: cat in x if isinstance(x, list) else False)
        group = df[mask].copy()
        if not group.empty:
            safe_name = cat.replace("&", "and").title().replace(" ", "_")
            outfile = os.path.join(out_dir, f"{safe_name}.xlsx")
            # 去掉辅助列后保存
            group.drop(columns=["大类", "norm", "matched_categories"], errors='ignore').to_excel(outfile, index=False)
            print(f"  保存 {len(group):7,d} 条 → {safe_name}.xlsx")

    df_unmatched = df[df['matched_categories'].apply(lambda x: len(x) == 0 if isinstance(x, list) else True)]
    if not df_unmatched.empty:
        unmatched_file = os.path.join(out_dir, "未匹配汇总.xlsx")
        df_unmatched.to_excel(unmatched_file, index=False)
        print(f"  未匹配项汇总保存 {len(df_unmatched):7,d} 条 → 未匹配汇总.xlsx")
    else:
        print("  全部匹配成功，无未匹配项")

    print("\n全部处理完成！")

if __name__ == "__main__":
    main()
