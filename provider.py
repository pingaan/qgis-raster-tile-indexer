from qgis.core import QgsProcessingProvider
from .algorithm import SelectRasterTiles


class RasterTileProvider(QgsProcessingProvider):
    def id(self):
        return 'rastertileindexer'

    def name(self):
        return 'Raster Tile Indexer'

    def longName(self):
        return self.name()

    def loadAlgorithms(self):
        self.addAlgorithm(SelectRasterTiles())
