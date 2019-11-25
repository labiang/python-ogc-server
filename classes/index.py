import io
import json
import os
from datetime import datetime

import geojson
import s2sphere
from Geometry import Point
from apscheduler.schedulers.background import BackgroundScheduler

from classes import wfs, tiles, geometry


class CollectionMetadata:
    name: str
    path: str
    last_modified: str

    def __init__(self, name, path, last_modified):
        self.name = name
        self.path = path
        self.last_modified = last_modified


class Collection:
    metadata: CollectionMetadata
    tile_cache: tiles.TileCache
    data_file: io.FileIO
    offset: [] = []
    bbox: [] = []
    web_mercator: [] = []
    id: [] = []
    by_id: {} = {}
    feature: [] = []

    def close(self):
        if self.data_file is not None:
            self.data_file.close()
            os.remove(self.data_file.name)


class Footer:
    links: [] = []
    bbox: [] = []


class Index:
    collections: {} = {}
    public_path: str

    def get_collection_metadata(self, path: str):
        for coll in self.collections:
            if coll.metadata.path == path:
                return coll.metadata

        return None

    def replace_collection(self, coll: Collection):
        old = self.collections.get(coll.metadata.name)

        if old is not None:
            self.collections[coll.metadata.name] = coll

    def get_collections(self):
        collections = []

        for collection in self.collections.values():
            collections.append(collection.metadata)

        return collections

    def get_items(self,
                  collection: str, start_id: str, start: int, limit: int,
                  bbox: s2sphere.LatLngRect, writer: io.BytesIO):
        if collection not in self.collections:
            from classes.server import HTTPResponses
            return None, None, HTTPResponses.NOT_FOUND

        coll = self.collections[collection]

        bounds = s2sphere.LatLngRect()
        skip = start
        num_features = 0

        writer.write(bytearray('{"type":"FeatureCollection","features":[', 'utf8'))
        for i, feature_bounds in enumerate(coll.bbox):
            if not bbox.intersects(feature_bounds):
                continue

            if num_features >= limit:
                next_id = coll.id[i]
                next_index = i
                break

            if skip > 0:
                skip = skip - 1
                break

            if num_features > 0:
                writer.write(bytearray(',', 'utf8'))

            writer.write(bytearray(coll.feature[i], encoding='utf8'))

            num_features += 1

            bounds = bounds.union(feature_bounds)

        writer.write(bytearray('],', 'utf8'))

        footer = Footer()

        self_link = wfs.WFSLink()
        self_link.rel = "self"
        self_link.title = "self"
        self_link.type = "application/geo+json"

        footer.bbox = geometry.encode_bbox(bounds)
        encoded_footer = json.dumps(footer.__dict__)

        writer.write(bytearray(encoded_footer[1:], 'utf8'))

        features = geojson.loads(writer.getvalue().decode('utf8'))

        return coll.metadata, features, None

    def get_item(self, collection: str, feature_id: str):
        if collection not in self.collections:
            from classes.server import HTTPResponses
            return None, HTTPResponses.NOT_FOUND

        coll = self.collections[collection]

        if feature_id not in coll.by_id:
            from classes.server import HTTPResponses
            return None, HTTPResponses.NOT_FOUND

        writer = io.BytesIO()
        coll_index = coll.by_id[feature_id]
        writer.write(bytearray(coll.feature[coll_index], encoding='utf8'))

        feature = geojson.loads(writer.getvalue().decode('utf8'))

        return feature, None

    def get_tile(self, collection: str, zoom: int, x: int, y: int):
        if x < 0 or y < 0 or not 0 < zoom < 30:
            from classes.server import HTTPResponses
            return None, CollectionMetadata, HTTPResponses.NOT_FOUND

        tile_key = tiles.TileKey(x=x, y=y, zoom=zoom)
        if collection not in self.collections:
            from classes.server import HTTPResponses
            return None, CollectionMetadata, HTTPResponses.NOT_FOUND

        coll = self.collections.get(collection)

        scale = 1 << zoom

        tile_bounds = geometry.get_tile_bounds(zoom, x, y)
        tile_origin = Point(x=(float(x) * 256.0 / float(scale)), y=(float(y) * 256.0 / float(scale)))
        tile = tiles.Tile()

        for i, feature_bounds in enumerate(coll.bbox):
            if not tile_bounds.intersects(feature_bounds):
                continue

            p = coll.web_mercator[i].__sub__(tile_origin).__mul__(float(scale))
            tile.draw_point(p)

        png = tile.to_png()

        return png, coll.metadata, None

    def reload_if_changed(self, cm: CollectionMetadata):
        coll, response = read_collection(cm.name, cm.path, cm.last_modified)

        from classes.server import HTTPResponses
        if response is not None and response is HTTPResponses.NOT_MODIFIED:
            return None
        else:
            self.replace_collection(coll)

    def watch_files(self):
        for collection in self.get_collections():
            self.reload_if_changed(collection)


def make_index(collections: dict, public_path: str):
    index = Index()
    index.public_path = public_path

    scheduler = BackgroundScheduler()

    scheduler.add_job(index.watch_files, 'interval', minutes=5)
    scheduler.start()

    for name, path in collections.items():
        coll, response = read_collection(name, path, datetime.min)
        index.collections[name] = coll

    return index


def read_collection(name, path, if_modified_since):
    abs_path = os.path.abspath(path)

    if not os.path.exists(abs_path):
        return None

    mod_time = datetime.fromtimestamp(os.path.getmtime(abs_path))

    if not mod_time > if_modified_since:
        from classes.server import HTTPResponses
        return None, HTTPResponses.NOT_MODIFIED

    with open(abs_path, "rb") as file:
        feature_collection = geojson.load(file)

    coll = Collection()
    coll.tile_cache = tiles.new_tile_cache(10000)
    coll.metadata = CollectionMetadata(name, path, mod_time)

    for i, f in enumerate(feature_collection.features):
        coll.id.append(f.id)
        coll.by_id[f.id] = i
        coll.feature.append(geojson.dumps(f, ensure_ascii=False, separators=(',', ':')))

        coll.bbox.append(geometry.compute_bounds(f.geometry))

        center = coll.bbox[i].get_center()
        coll.web_mercator.append(geometry.project_web_mercator(center))

    return coll, None
