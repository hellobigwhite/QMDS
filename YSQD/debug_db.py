from pymongo import MongoClient
client = MongoClient('mongodb://localhost:27017/', serverSelectionTimeoutMS=5000)
try:
    client.server_info()
    db = client['shopify_data_new']
    names = db.list_collection_names()
    print('Total collections:', len(names))
    for name in sorted(names):
        cnt = db[name].estimated_document_count()
        print(name, cnt)
except Exception as e:
    print('Error:', e)
client.close()