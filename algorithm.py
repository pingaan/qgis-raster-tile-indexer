
"""QGIS Processing: lightweight centroid CSV lookup for regular, north-up raster tiles.

The source raster is opened once per file ONLY when building an index. On reused
indices, only one reference raster is opened to determine dimensions and CRS.
"""

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
)


EXTENSIONS = {'.tif', '.tiff', '.img', '.jp2', '.bil'}
INDEX_NAME = 'raster_tile_index.csv'
HEADER = ['FILENAME', 'x', 'y']


def _files(root, recursive):
    walk = root.rglob('*') if recursive else root.iterdir()
    return sorted(
        p for p in walk
        if p.is_file() and p.suffix.lower() in EXTENSIONS
    )


def _tile_metadata(path):
    ds = gdal.OpenEx(
        str(path),
        gdal.OF_RASTER | gdal.OF_READONLY
    )

    if ds is None:
        raise ValueError(f'GDAL cannot open raster: {path}')

    try:
        gt = ds.GetGeoTransform(can_return_null=True)
        wkt = ds.GetProjectionRef()

        if not gt or not wkt or ds.RasterXSize < 1 or ds.RasterYSize < 1:
            raise ValueError(f'Missing georeferencing: {path}')

        if (
            abs(gt[2]) > 1e-9
            or abs(gt[4]) > 1e-9
            or gt[1] <= 0
            or gt[5] >= 0
        ):
            raise ValueError(
                'Optimised index requires north-up, unrotated raster tiles.'
            )

        width = gt[1] * ds.RasterXSize
        height = -gt[5] * ds.RasterYSize

        cx = gt[0] + width / 2
        cy = gt[3] - height / 2

        return cx, cy, width, height, wkt

    finally:
        ds = None


def _build_index(root, index_path, recursive, feedback):
    files = _files(root, recursive)

    if not files:
        raise QgsProcessingException(
            'No supported raster tiles found in the directory.'
        )

    feedback.pushInfo(
        f'Building minimal centroid index from {len(files)} rasters ...'
    )

    rows = []
    reference = None

    for i, path in enumerate(files):
        if feedback.isCanceled():
            raise QgsProcessingException(
                'Cancelled; existing CSV left unchanged.'
            )

        try:
            cx, cy, width, height, wkt = _tile_metadata(path)

            if reference is None:
                reference = (width, height, wkt)

            elif (
                abs(width - reference[0]) > 1e-5
                or abs(height - reference[1]) > 1e-5
                or wkt != reference[2]
            ):
                raise QgsProcessingException(
                    'Tiles have different extents or CRS. '
                    'This regular-grid mode requires one tile size and CRS.'
                )

            rows.append((
                path.relative_to(root).as_posix(),
                cx,
                cy
            ))

        except QgsProcessingException:
            raise

        except Exception as exc:
            feedback.pushWarning(
                f'Skipping unreadable raster {path}: {exc}'
            )

        feedback.setProgress(
            40 * (i + 1) / len(files)
        )

    if not rows:
        raise QgsProcessingException(
            'No georeferenced rasters could be indexed.'
        )

    fd, temporary = tempfile.mkstemp(
        dir=str(index_path.parent),
        prefix='.raster_index_',
        suffix='.csv'
    )

    os.close(fd)

    try:
        with open(
            temporary,
            'w',
            encoding='utf-8',
            newline=''
        ) as f:
            writer = csv.writer(f)
            writer.writerow(HEADER)
            writer.writerows(rows)

        os.replace(temporary, index_path)

    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

    feedback.pushInfo(
        f'Created {index_path} ({len(rows)} rows, 3 columns).'
    )


class SelectRasterTiles(QgsProcessingAlgorithm):

    POLYGONS = 'POLYGONS'
    FOLDER = 'FOLDER'
    CSV = 'CSV'
    RECURSIVE = 'RECURSIVE'
    REBUILD = 'REBUILD'
    BUFFER = 'BUFFER'
    LOAD = 'LOAD'

    FILES = 'FILES'
    INDEX_PATH = 'INDEX_PATH'
    COUNT = 'COUNT'

    def name(self):
        return 'selectrastertiles'

    def displayName(self):
        return 'Select regular raster tiles by polygon (centroid CSV)'

    def group(self):
        return 'Raster discovery'

    def groupId(self):
        return 'rasterdiscovery'

    def createInstance(self):
        return SelectRasterTiles()

    def shortHelpString(self):
        return (
            'For collections of identically-sized, north-up raster tiles '
            'in one CRS. Creates/reuses a 3-column FILENAME,x,y centroid CSV. '
            'Also accepts older CSV files with an Unnamed: 0 column. '
            'Opens one reference raster to infer tile size and CRS; '
            'then reconstructs rectangles mathematically and selects '
            'those intersecting input polygons. Existing CSV coordinates '
            'MUST represent raster centroids. Does not copy or clip rasters. '
            'Rebuild the index if files change.'
        )

    def initAlgorithm(self, config=None):

        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.POLYGONS,
                'Input polygon layer / file',
                [QgsProcessing.TypeVectorPolygon]
            )
        )

        self.addParameter(
            QgsProcessingParameterFile(
                self.FOLDER,
                'Directory with raster tiles',
                behavior=QgsProcessingParameterFile.Folder
            )
        )

        self.addParameter(
            QgsProcessingParameterFile(
                self.CSV,
                'Existing CSV index (optional; leave blank for auto)',
                behavior=QgsProcessingParameterFile.File,
                fileFilter='CSV files (*.csv)',
                optional=True
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.RECURSIVE,
                'Search subdirectories',
                defaultValue=True
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.REBUILD,
                'Rebuild index',
                defaultValue=False
            )
        )

        self.addParameter(
            QgsProcessingParameterDistance(
                self.BUFFER,
                'Buffer polygons (metres in projected CRS)',
                defaultValue=0,
                parentParameterName=self.POLYGONS,
                minValue=0
            )
        )

        self.addParameter(
            QgsProcessingParameterBoolean(
                self.LOAD,
                'Load matching rasters in Raster Tiles group',
                defaultValue=True
            )
        )

        self.addOutput(
            QgsProcessingOutputNumber(
                self.COUNT,
                'Matching tiles'
            )
        )

        self.addOutput(
            QgsProcessingOutputString(
                self.INDEX_PATH,
                'Index path'
            )
        )

        self.addOutput(
            QgsProcessingOutputString(
                self.FILES,
                'Matched raster paths'
            )
        )

    def processAlgorithm(self, parameters, context, feedback):

        src = self.parameterAsSource(
            parameters,
            self.POLYGONS,
            context
        )

        if src is None or not src.sourceCrs().isValid():
            raise QgsProcessingException(
                'Polygon source requires a valid CRS.'
            )

        root = Path(
            self.parameterAsString(
                parameters,
                self.FOLDER,
                context
            )
        ).expanduser().resolve()

        if not root.is_dir():
            raise QgsProcessingException(
                'Raster directory not found.'
            )

        user_index = self.parameterAsString(
            parameters,
            self.CSV,
            context
        ).strip()

        index_path = (
            Path(user_index).expanduser().resolve()
            if user_index
            else root / INDEX_NAME
        )

        dist = self.parameterAsDouble(
            parameters,
            self.BUFFER,
            context
        )

        if dist and src.sourceCrs().isGeographic():
            raise QgsProcessingException(
                'Use a projected polygon CRS for distance buffering.'
            )

        polygon_geoms = []

        for feature in src.getFeatures():
            g = feature.geometry()

            if g and not g.isEmpty():
                g = QgsGeometry(g)

                if dist:
                    g = g.buffer(dist, 8)

                polygon_geoms.append(g)

        if not polygon_geoms:
            raise QgsProcessingException(
                'No polygons to select from.'
            )

        recursive = self.parameterAsBoolean(
            parameters,
            self.RECURSIVE,
            context
        )

        rebuild = self.parameterAsBoolean(
            parameters,
            self.REBUILD,
            context
        )

        if rebuild or not index_path.exists():
            _build_index(
                root,
                index_path,
                recursive,
                feedback
            )
        else:
            feedback.pushInfo(
                f'Reusing centroid index: {index_path}'
            )
            feedback.setProgress(40)

        with index_path.open(
            'r',
            newline='',
            encoding='utf-8-sig'
        ) as f:
            rows = list(csv.DictReader(f))

        if not rows or not all(
            k in rows[0] for k in HEADER
        ):
            raise QgsProcessingException(
                'CSV must contain FILENAME,x,y columns.'
            )

        # Read only ONE raster to establish tile dimensions and CRS.
        first_path = root / rows[0]['FILENAME']

        if not first_path.is_file():
            raise QgsProcessingException(
                f'First indexed raster not found: {first_path}'
            )

        first_cx, first_cy, width, height, wkt = _tile_metadata(
            first_path
        )

        if (
            abs(first_cx - float(rows[0]['x'])) > 0.01
            or abs(first_cy - float(rows[0]['y'])) > 0.01
        ):
            raise QgsProcessingException(
                'Index x,y does not match the reference raster centroid. '
                'Confirm coordinate convention before using this index.'
            )

        tile_crs = QgsCoordinateReferenceSystem()
        tile_crs.createFromWkt(wkt)

        if not tile_crs.isValid():
            raise QgsProcessingException(
                'Reference raster CRS is invalid.'
            )

        transformer = QgsCoordinateTransform(
            src.sourceCrs(),
            tile_crs,
            context.transformContext()
        )

        transformed = []

        for g in polygon_geoms:
            clone = QgsGeometry(g)
            clone.transform(transformer)
            transformed.append(clone)

        bounds = [
            g.boundingBox() for g in transformed
        ]

        matches = []

        for i, row in enumerate(rows):

            if feedback.isCanceled():
                return {}

            try:
                cx = float(row['x'])
                cy = float(row['y'])

                left = cx - width / 2
                right = cx + width / 2
                bottom = cy - height / 2
                top = cy + height / 2

                # First: fast bounding-box comparison.
                candidates = [
                    g
                    for g, b in zip(transformed, bounds)
                    if not (
                        right < b.xMinimum()
                        or left > b.xMaximum()
                        or top < b.yMinimum()
                        or bottom > b.yMaximum()
                    )
                ]

                # Second: exact polygon-to-tile intersection.
                if candidates:

                    corners = [
                        QgsPointXY(left, bottom),
                        QgsPointXY(right, bottom),
                        QgsPointXY(right, top),
                        QgsPointXY(left, top),
                        QgsPointXY(left, bottom)
                    ]

                    rect = QgsGeometry.fromPolygonXY([corners])

                    if any(
                        g.intersects(rect)
                        for g in candidates
                    ):
                        path = root / row['FILENAME']

                        if path.is_file():
                            matches.append(
                                str(path.resolve())
                            )
                        else:
                            feedback.pushWarning(
                                f'Indexed file is missing: {path}'
                            )

            except (ValueError, TypeError) as exc:
                feedback.pushWarning(
                    f'Skipping invalid CSV row {i + 2}: {exc}'
                )

            feedback.setProgress(
                40 + 60 * (i + 1) / len(rows)
            )

        self._matched_paths = list(
            dict.fromkeys(matches)
        )

        self._load_in_project = self.parameterAsBoolean(
            parameters,
            self.LOAD,
            context
        )

        feedback.pushInfo(
            f'Matched {len(self._matched_paths)} '
            f'of {len(rows)} indexed tiles.'
        )

        return {
            self.COUNT: len(self._matched_paths),
            self.INDEX_PATH: str(index_path),
            self.FILES: '\n'.join(self._matched_paths)
        }

    def postProcessAlgorithm(self, context, feedback):

        if not getattr(self, '_load_in_project', False):
            return {}

        project = context.project() or QgsProject.instance()
        tree = project.layerTreeRoot()

        group = (
            tree.findGroup('Raster Tiles')
            or tree.addGroup('Raster Tiles')
        )

        existing = {
            os.path.normcase(os.path.normpath(l.source()))
            for l in project.mapLayers().values()
            if isinstance(l, QgsRasterLayer)
        }

        count = 0

        for path in getattr(self, '_matched_paths', []):

            key = os.path.normcase(
                os.path.normpath(path)
            )

            if key in existing:
                continue

            layer = QgsRasterLayer(
                path,
                Path(path).stem
            )

            if not layer.isValid():
                feedback.pushWarning(
                    f'Failed to load raster: {path}'
                )
                continue

            project.addMapLayer(layer, False)
            group.addLayer(layer)

            existing.add(key)
            count += 1

        feedback.pushInfo(
            f'Loaded {count} new layers into Raster Tiles.'
        )

        return {}
