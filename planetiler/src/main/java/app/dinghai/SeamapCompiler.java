// SPDX-License-Identifier: LGPL-3.0-only
package app.dinghai;

import com.onthegomap.planetiler.FeatureCollector;
import com.onthegomap.planetiler.FeatureCollector.Feature;
import com.onthegomap.planetiler.Planetiler;
import com.onthegomap.planetiler.Profile;
import com.onthegomap.planetiler.config.Arguments;
import com.onthegomap.planetiler.geo.GeoUtils;
import com.onthegomap.planetiler.reader.SourceFeature;
import com.onthegomap.planetiler.util.ZoomFunction;
import java.nio.file.Path;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Consumer;

/** Compiles DingHai's App-facing marine layers directly from OSM and OSM land polygons. */
public final class SeamapCompiler implements Profile {
  static final int MIN_ZOOM = 8;
  static final int MAX_ZOOM = 14;
  private static final Set<String> COASTAL_WETLAND = Set.of("mangrove", "tidalflat", "saltmarsh");
  private static final Set<String> STRUCTURES = Set.of("pier", "breakwater", "groyne", "quay", "wharf", "pontoon");
  private static final Set<String> WATER = Set.of("reservoir", "basin", "water");

  private final String osmUrl;
  private final String landUrl;
  private final AtomicLong landSeen = new AtomicLong();
  private final AtomicLong landEmitted = new AtomicLong();

  private SeamapCompiler(String osmUrl, String landUrl) {
    this.osmUrl = osmUrl;
    this.landUrl = landUrl;
  }

  private static String value(SourceFeature source, String key) {
    Object value = source.getTag(key);
    return value == null ? null : value.toString();
  }

  private static String colours(String value) {
    return value == null ? null : value.toLowerCase().replace(';', '_').replace(' ', '_');
  }

  private static Feature copyTags(Feature feature, SourceFeature source) {
    feature.setAttr("osm_id", source.id()).setAttr("zoom", (ZoomFunction<Integer>) zoom -> zoom);
    for (var tag : source.tags().entrySet()) {
      String key = tag.getKey();
      if (key.startsWith("seamark:") || key.equals("name") || key.equals("name:zh") ||
        key.equals("name:zh-Hans") || key.equals("function")) {
        feature.setAttr(key, tag.getValue());
      }
    }
    return feature;
  }

  private static Feature seamark(Feature feature, SourceFeature source, String type, String category) {
    copyTags(feature, source);
    String prefix = "seamark:" + type + ":";
    feature.setAttr("type", type)
      .setAttr("category", category != null ? category : value(source, prefix + "category"))
      .setAttr("shape", value(source, prefix + "shape"))
      .setAttr("color", colours(value(source, prefix + "colour")))
      .setAttr("color_pattern", value(source, prefix + "colour_pattern"))
      .setAttr("topmark_shape", value(source, "seamark:topmark:shape"))
      .setAttr("topmark_color", colours(value(source, "seamark:topmark:colour")))
      .setAttr("light_color", colours(value(source, "seamark:light:colour")))
      .setAttr("family", "light_major".equals(type) ? "major_aid" : null)
      .setZoomRange(MIN_ZOOM, MAX_ZOOM)
      .setMinPixelSizeAtAllZooms(0)
      .setBufferPixels(16);
    return feature;
  }

  @Override
  public void processFeature(SourceFeature source, FeatureCollector features) {
    if ("land_polygons".equals(source.getSource()) || "land_polygons".equals(source.getSourceLayer())) {
      landSeen.incrementAndGet();
      if (source.canBePolygon()) {
        landEmitted.incrementAndGet();
        features.polygon("land").setZoomRange(MIN_ZOOM, MAX_ZOOM);
      }
      return;
    }

    String type = value(source, "seamark:type");
    if (type != null) {
      seamark(features.anyGeometry("seamark"), source, type, null);
    }

    String manMade = value(source, "man_made");
    if (manMade != null && STRUCTURES.contains(manMade) && !"shoreline_construction".equals(type) && !"pontoon".equals(type)) {
      String structureType = "pontoon".equals(manMade) ? "pontoon" : "shoreline_construction";
      seamark(features.anyGeometry("seamark"), source, structureType, manMade);
    }

    String natural = value(source, "natural");
    String waterway = value(source, "waterway");
    String landuse = value(source, "landuse");
    String water = value(source, "water");
    if (source.canBePolygon() && ("water".equals(natural) || "riverbank".equals(waterway) ||
      "reservoir".equals(landuse) || "basin".equals(landuse) || (water != null && WATER.contains(water)))) {
      Feature feature = features.polygon("water").setZoomRange(MIN_ZOOM, MAX_ZOOM);
      feature.inheritAttrsFromSource("name", "water");
    }

    String wetland = value(source, "wetland");
    if (source.canBePolygon() && "wetland".equals(natural) && wetland != null && COASTAL_WETLAND.contains(wetland)) {
      Feature feature = features.polygon("wetland").setZoomRange(MIN_ZOOM, MAX_ZOOM)
        .setAttr("type", "wetland").setAttr("category", wetland).setMinPixelSizeAtAllZooms(0);
      feature.inheritAttrsFromSource("name", "surface");
    }

    if ("sea".equals(value(source, "place")) || "bay".equals(natural)) {
      Feature feature = (source.canBePolygon() ? features.pointOnSurface("sea_area") : features.anyGeometry("sea_area"))
        .setZoomRange(MIN_ZOOM, MAX_ZOOM).setAttr("category", "bay".equals(natural) ? "bay" : "sea");
      feature.inheritAttrsFromSource("name", "name:zh", "name:zh-Hans");
    }
  }

  @Override public String name() { return "DingHai Seamap OSM"; }
  @Override public String description() { return "DingHai marine layers compiled from OpenStreetMap"; }
  @Override public boolean isOverlay() { return true; }
  @Override public String attribution() { return OSM_ATTRIBUTION; }
  @Override public String version() { return "seamap-osm-1"; }
  @Override public boolean caresAboutWikidataTranslation(com.onthegomap.planetiler.reader.osm.OsmElement elem) { return false; }

  @Override
  public void finish(String sourceName, FeatureCollector.Factory featureCollectors, Consumer<FeatureCollector.Feature> next) {
    if ("land_polygons".equals(sourceName) && (landSeen.get() == 0 || landEmitted.get() == 0)) {
      throw new IllegalStateException("land polygon stage produced no polygons: seen=" + landSeen + ", emitted=" + landEmitted);
    }
  }

  @Override
  public Map<String, String> extraArchiveMetadata() {
    return Map.of(
      "dinghai:generator", "seamap-osm-1",
      "dinghai:osmSource", osmUrl,
      "dinghai:landPolygonSource", landUrl,
      "dinghai:license", "ODbL-1.0",
      "dinghai:licenseURL", "https://opendatacommons.org/licenses/odbl/1-0/"
    );
  }

  public static void main(String[] argv) throws Exception {
    Arguments args = Arguments.fromArgsOrConfigFile(argv);
    var requestedBounds = args.bounds("bounds", "bounds");
    if (requestedBounds != null && !requestedBounds.equals(GeoUtils.WORLD_LAT_LON_BOUNDS)) {
      throw new IllegalArgumentException("Pre-clip OSM and land Shapefile inputs; regional bounds drop land polygons");
    }
    args = args.withDefault("bounds", "-180,-85.0511287798066,180,85.0511287798066");
    Path osm = Path.of(args.getString("osm", "local OSM PBF input", "planet.osm.pbf"));
    Path land = Path.of(args.getString("land", "extracted OSM land_polygons.shp", "land_polygons.shp"));
    if (land.toString().toLowerCase().endsWith(".zip")) {
      throw new IllegalArgumentException("Unzip land polygons and pass land_polygons.shp; ZIP input is not supported");
    }
    Path output = Path.of(args.getString("output", "output PMTiles", "seamap.pmtiles"));
    String osmUrl = args.getString("osm_url", "OSM source URL", "https://planet.openstreetmap.org/pbf/planet-latest.osm.pbf");
    String landUrl = args.getString("land_url", "land polygon source URL",
      "https://osmdata.openstreetmap.de/download/land-polygons-complete-4326.zip");
    Planetiler.create(args)
      .setProfile(new SeamapCompiler(osmUrl, landUrl))
      .addOsmSource("osm", osm, osmUrl)
      .addShapefileSource("EPSG:4326", "land_polygons", land, landUrl)
      .overwriteOutput(output)
      .run();
  }
}
