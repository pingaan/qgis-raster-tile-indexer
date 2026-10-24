"""Native QGIS Processing algorithm for reusable local raster metadata lookup."""
import csv
import os
import tempfile
from pathlib import Path

from osgeo import gdal
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsPointXY,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingOutputNumber,
    QgsProcessingOutputString,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterDistance,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterFile,
    QgsProject,
    QgsRasterLayer,
    QgsWkbTypes,
)

EXTENSIONS = {'.tif', '.tiff', '.img', '.vrt', '.jp2', '.asc', '.bil'}
HEADER = ['relative_path', 'crs_wkt', 'min_x', 'min_y', 'max_x', 'max_y',
          'corner_0_x', 'corner_0_y', 'corner_1_x', 'corner_1_y',
          'corner_2_x', 'corner_2_y', 'corner_3_x', 'corner_3_y',
          'file_bytes', 'modified_ns']
INDEX_NAME = 'raster_tile_index.csv'


def _indexed_record(path, root):
    ds = gdal.OpenEx(str(path), gdal.OF_RASTER | gdal.OF_READONLY)
    if ds is None:
        return None
    try:
        gt = ds.GetGeoTransform(can_return_null=True)
        projection = ds.GetProjectionRef()
        if gt is None or not projection or ds.RasterXSize <= 0 or ds.RasterYSize <= 0:
            return None
        def xy(col, row):
            return gt[0] + col * gt[1] + row * gt[2], gt[3] + col * gt[4] + row * gt[5]
        corners = [xy(0, 0), xy(ds.RasterXSize, 0),
                   xy(ds.RasterXSize, ds.RasterYSize), xy(0, ds.RasterYSize)]
        xs, ys = zip(*corners)
        st = path.stat()
        rec = {'relative_path': path.relative_to(root).as_posix(),
               'crs_wkt': projection, 'min_x': min(xs), 'min_y': min(ys),
               'max_x': max(xs), 'max_y': max(ys),
               'file_bytes': st.st_size, 'modified_ns': st.st_mtime_ns}
        for i, (x, y) in enumerate(corners):
            rec[f'corner_{i}_x'] = x
            rec[f'corner_{i}_y'] = y
        return rec
    finally:
        ds = None


def _build_index(root, index_path, recursive, feedback):
    walk = root.rglob('*') if recursive else root.iterdir()
    files = sorted(p for p in walk if p.is_file() and p.suffix.lower() in EXTENSIONS)
    feedback.pushInfo(f'Indexing {len(files)} candidate raster files...')
    fd, tmpname = tempfile.mkstemp(prefix='.raster_tile_index_', suffix='.csv', dir=str(root))
    os.close(fd)
    count, skipped = 0, 0
    try:
        with open(tmpname, 'w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=HEADER)
            writer.writeheader()
            for i, path in enumerate(files):
                if feedback.isCanceled():
                    raise QgsProcessingException('Index building canceled; existing index unchanged.')
                try:
                    record = _indexed_record(path, root)
                    if record is None:
                        skipped += 1
                        feedback.pushWarning(f'Skipped missing spatial reference: {path}')
                    else:
                        writer.writerow(record)
                        count += 1
                except Exception as exc:
                    skipped += 1
                    feedback.pushWarning(f'Cannot index {path}: {exc}')
                feedback.setProgress(40.0 * (i + 1) / max(len(files), 1))
        os.replace(tmpname, index_path)
    finally:
        if os.path.exists(tmpname):
            os.unlink(tmpname)
    feedback.pushInfo(f'CSV index written: {count} indexed, {skipped} skipped.')
    return count


def _make_footprint(rec):
    coords = [QgsPointXY(float(rec[f'corner_{i}_x']), float(rec[f'corner_{i}_y']))
              for i in range(4)]
    return QgsGeometry.fromPolygonXY([coords + [coords[0]]])


class SelectRasterTiles(QgsProcessingAlgorithm):
    POLYGONS = 'POLYGONS'
    FOLDER = 'FOLDER'
    RECURSIVE = 'RECURSIVE'
    REBUILD = 'REBUILD'
    BUFFER = 'BUFFER'
    LOAD = 'LOAD'
    MODE = 'MODE'
    FILES = 'FILES'
    INDEX_PATH = 'INDEX_PATH'
    COUNT = 'COUNT'

    def name(self):
        return 'selectrastertiles'

    def displayName(self):
        return 'Select raster tiles by polygon (CSV index)'

    def group(self):
        return 'Raster discovery'

    def groupId(self):
        return 'rasterdiscovery'

    def createInstance(self):
        return SelectRasterTiles()

    def shortHelpString(self):
        return ("Choose a polygon layer (loaded or browsed from disk), a raster folder, and optional "
                "buffer. The tool creates or reuses raster_tile_index.csv in the raster folder, "
                "identifies raster footprints intersecting any input polygon, and can load those "
                "original rasters into the project's 'Raster Tiles' group. It does not clip or "
                "copy raster files. The buffer uses layer CRS units: reproject geographic layers "
                "to a suitable projected CRS before using metre offsets. Force rebuild when raster "
                "files have been added, moved or modified since index creation. A footprint test "
                "is used rather than centroid-within-polygon so slender polygons are not missed.")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.POLYGONS, 'Input polygon layer or file (one or more features)',
            [QgsProcessing.TypeVectorPolygon]))
        self.addParameter(QgsProcessingParameterFile(
            self.FOLDER, 'Folder containing raster tiles',
            behavior=QgsProcessingParameterFile.Folder))
        self.addParameter(QgsProcessingParameterBoolean(
            self.RECURSIVE, 'Search raster subfolders', defaultValue=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.REBUILD, 'Rebuild CSV index (if raster collection has changed)', defaultValue=False))
        self.addParameter(QgsProcessingParameterDistance(
            self.BUFFER, 'Buffer around polygons (layer CRS units)', defaultValue=0.0,
            parentParameterName=self.POLYGONS, minValue=0.0))
        self.addParameter(QgsProcessingParameterBoolean(
            self.LOAD, 'Add matching raster tiles to Raster Tiles group', defaultValue=True))
        self.addOutput(QgsProcessingOutputNumber(self.COUNT, 'Number of matched tiles'))
        self.addOutput(QgsProcessingOutputString(self.INDEX_PATH, 'CSV index path'))
        self.addOutput(QgsProcessingOutputString(self.FILES, 'Matched raster file paths'))

    def processAlgorithm(self, parameters, context, feedback):
        src = self.parameterAsSource(parameters, self.POLYGONS, context)
        if src is None:
            raise QgsProcessingException('Choose a polygon layer or file.')
        root = Path(self.parameterAsString(parameters, self.FOLDER, context)).expanduser().resolve()
        if not root.is_dir():
            raise QgsProcessingException(f'Raster directory does not exist: {root}')
        buffer_dist = self.parameterAsDouble(parameters, self.BUFFER, context)
        if buffer_dist and src.sourceCrs().isGeographic():
            raise QgsProcessingException('Buffer on geographic CRS is in degrees; reproject polygons to a projected CRS first.')
        geometries = []
        for feat in src.getFeatures():
            if feedback.isCanceled():
                return {}
            geom = feat.geometry()
            if geom and not geom.isEmpty():
                geom = QgsGeometry(geom)
                if buffer_dist:
                    geom = geom.buffer(buffer_dist, 8)
                if not geom.isEmpty():
                    geometries.append(geom)
        if not geometries:
            raise QgsProcessingException('Input contains no nonempty polygons (check selection).')
        index_path = root / INDEX_NAME
        if self.parameterAsBoolean(parameters, self.REBUILD, context) or not index_path.is_file():
            _build_index(root, index_path,
                         self.parameterAsBoolean(parameters, self.RECURSIVE, context), feedback)
        else:
            feedback.pushInfo(f'Reusing existing index: {index_path}. Rebuild if tile files changed.')
            feedback.setProgress(40)
        matches, scanned, stale = [], 0, 0
        transforms = {}
        source_crs = src.sourceCrs()
        if not source_crs.isValid():
            raise QgsProcessingException('Input polygon layer has no valid coordinate reference system.')
        with open(index_path, 'r', newline='', encoding='utf-8') as stream:
            reader = csv.DictReader(stream)
            if not set(HEADER).issubset(set(reader.fieldnames or [])):
                raise QgsProcessingException('Index CSV has incompatible columns. Tick Rebuild CSV index.')
            rows = list(reader)
        for i, rec in enumerate(rows):
            if feedback.isCanceled():
                return {}
            scanned += 1
            path = root / rec['relative_path']
            if not path.is_file():
                stale += 1
                continue
            try:
                tile_crs = QgsCoordinateReferenceSystem()
                tile_crs.createFromWkt(rec['crs_wkt'])
                if not tile_crs.isValid():
                    raise ValueError('unrecognised raster CRS')
                crs_key = rec['crs_wkt']
                if crs_key not in transforms:
                    transformer = QgsCoordinateTransform(source_crs, tile_crs, context.transformContext())
                    transformed = []
                    for original in geometries:
                        clone = QgsGeometry(original)
                        clone.transform(transformer)
                        transformed.append(clone)
                    transforms[crs_key] = transformed
                footprint = _make_footprint(rec)
                if any(g.boundingBox().intersects(footprint.boundingBox()) and g.intersects(footprint)
                       for g in transforms[crs_key]):
                    matches.append(str(path))
            except Exception as exc:
                feedback.pushWarning(f'Failed CRS or geometry check for {path}: {exc}')
            feedback.setProgress(40 + 60 * (i + 1) / max(len(rows), 1))
        if stale:
            feedback.pushWarning(f'{stale} indexed files no longer exist; rebuild the index.')
        self._matched_paths = matches
        self._load_in_project = self.parameterAsBoolean(parameters, self.LOAD, context)
        feedback.pushInfo(f'Matched {len(matches)} raster files of {scanned} indexed files.')
        return {self.COUNT: len(matches), self.INDEX_PATH: str(index_path),
                self.FILES: '\n'.join(matches)}

    def postProcessAlgorithm(self, context, feedback):
        """Project manipulation must occur on the main thread, after Processing work."""
        if not getattr(self, '_load_in_project', False):
            return {}
        project = context.project() or QgsProject.instance()
        root = project.layerTreeRoot()
        group = root.findGroup('Raster Tiles')
        if group is None:
            group = root.addGroup('Raster Tiles')
        existing = {os.path.normcase(os.path.normpath(l.source()))
                    for l in project.mapLayers().values() if isinstance(l, QgsRasterLayer)}
        added = 0
        for path in getattr(self, '_matched_paths', []):
            normalized = os.path.normcase(os.path.normpath(path))
            if normalized in existing:
                feedback.pushInfo(f'Already loaded: {path}')
                continue
            layer = QgsRasterLayer(path, Path(path).stem)
            if not layer.isValid():
                feedback.pushWarning(f'Could not load tile: {path}')
                continue
            project.addMapLayer(layer, False)
            group.addLayer(layer)
            existing.add(normalized)
            added += 1
        feedback.pushInfo(f'Loaded {added} new raster layer(s) into Raster Tiles.')
        return {}
