def classFactory(iface):
    from .plugin import RasterTileIndexerPlugin
    return RasterTileIndexerPlugin(iface)
