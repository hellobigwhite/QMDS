import requests, warnings, json
warnings.filterwarnings('ignore')

# Try some common domains to find one that's actually Shopify
domains = [
    "shop.boatspout.com", "store.boatspout.com",
    "shop.grillplateco.com", "store.grillplateco.com",
    "shop.classicreelia.com",
    "kyliecosmetics.com", "colourpop.com", "allbirds.com",
    "gymshark.com", "shop.ankershop.com",
    "hydra-store.com", "liforme.com",
]
for d in domains:
    for prefix in ["https://", "https://www."]:
        url = prefix + d + "/products.json"
        try:
            r = requests.get(url, timeout=5, verify=False)
            if r.status_code == 200:
                data = r.json()
                count = len(data.get("products", []))
                print(f"FOUND SHOPIFY: {url} - {count} products")
                raise SystemExit(0)
            elif r.status_code == 301 or r.status_code == 302:
                pass  # redirect
        except:
            pass

print("No Shopify stores found")
