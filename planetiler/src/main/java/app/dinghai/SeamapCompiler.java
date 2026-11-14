// SPDX-License-Identifier: LGPL-3.0-only
package app.dinghai;

import com.onthegomap.planetiler.FeatureCollector;
import com.onthegomap.planetiler.FeatureCollector.Feature;
import com.onthegomap.planetiler.FeatureMerge;
import com.onthegomap.planetiler.Planetiler;
import com.onthegomap.planetiler.Profile;
import com.onthegomap.planetiler.VectorTile;
import com.onthegomap.planetiler.config.Arguments;
import com.onthegomap.planetiler.geo.GeoUtils;
import com.onthegomap.planetiler.geo.GeometryException;
import com.onthegomap.planetiler.geo.TileCoord;
import com.onthegomap.planetiler.reader.SourceFeature;
import com.onthegomap.planetiler.util.Parse;
import com.onthegomap.planetiler.util.ZoomFunction;
import java.math.BigDecimal;
import java.math.BigInteger;
import java.math.RoundingMode;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Consumer;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.locationtech.jts.geom.Geometry;
import org.locationtech.jts.geom.Polygon;
import org.locationtech.jts.geom.util.GeometryFixer;
import org.locationtech.jts.operation.overlayng.OverlayNG;
import org.locationtech.jts.operation.overlayng.OverlayNGRobust;

/**
 * Compiles every DingHai Seamap layer from one OSM snapshot, the OSM land polygons and the
 * GEBCO-derived depth bands of {@code depth_product.py}.
 */
public final class SeamapCompiler implements Profile {
  static final int MIN_ZOOM = 8;
  static final int MAX_ZOOM = 14;
  /** z0-7 is the global overview: land and inland water only, simplified a little more. */
  private static final double OVERVIEW_PIXEL_TOLERANCE = 0.25;
  private static final double OVERVIEW_MIN_LAKE_PIXELS = 2;
  private static final Set<String> RIVER_WATER = Set.of("river", "canal", "stream", "ditch", "drain", "oxbow");
  /** landuse, roads, peaks, places and depth stop here, as the App style expects. */
  static final int LAND_MAX_ZOOM = 12;
  private static final Set<String> COASTAL_WETLAND = Set.of("mangrove", "tidalflat", "saltmarsh");
  private static final Set<String> STRUCTURES = Set.of("pier", "breakwater", "groyne", "quay", "wharf", "pontoon");
  private static final Set<String> WATER = Set.of("reservoir", "basin", "water");
  private static final Map<String, String> LANDUSE = Map.ofEntries(
    Map.entry("landuse=forest", "wood"), Map.entry("natural=wood", "wood"), Map.entry("natural=scrub", "wood"),
    Map.entry("leisure=park", "park"), Map.entry("leisure=garden", "park"), Map.entry("leisure=golf_course", "park"),
    Map.entry("landuse=grass", "park"), Map.entry("landuse=meadow", "park"), Map.entry("landuse=village_green", "park"),
    Map.entry("landuse=recreation_ground", "park"), Map.entry("natural=grassland", "park"),
    Map.entry("landuse=farmland", "farmland"), Map.entry("landuse=orchard", "farmland"),
    Map.entry("landuse=farmyard", "farmland"), Map.entry("landuse=allotments", "farmland"),
    Map.entry("landuse=residential", "urban"), Map.entry("landuse=commercial", "urban"),
    Map.entry("landuse=retail", "urban"), Map.entry("landuse=industrial", "industrial"),
    Map.entry("landuse=brownfield", "industrial"), Map.entry("natural=beach", "beach"),
    Map.entry("natural=sand", "beach"));
  private static final Map<String, String> ROADS = Map.ofEntries(
    Map.entry("motorway", "highway"), Map.entry("motorway_link", "highway"),
    Map.entry("trunk", "major"), Map.entry("trunk_link", "major"),
    Map.entry("primary", "major"), Map.entry("primary_link", "major"),
    Map.entry("secondary", "secondary"), Map.entry("secondary_link", "secondary"),
    Map.entry("tertiary", "secondary"), Map.entry("tertiary_link", "secondary"));
  /** First zoom of each place kind: the App draws it from there. */
  private static final Map<String, Integer> PLACES = Map.of("town", 9, "village", 11, "locality", 12);
  /** {@code seamark:light:N:*}: the numbered lights (sectors, directions) of one seamark. */
  private static final Pattern NUMBERED_LIGHT = Pattern.compile("^seamark:light:(\\d+):");
  /** Plain decimal degrees; {@link Double#parseDouble} would also take "245d", "1e2" or "NaN". */
  private static final Pattern DEGREES = Pattern.compile("^(\\d+(\\.\\d*)?|\\.\\d+)$");
  /**
   * OSM light colours as the glow colour codes of {@code dh_sectors} (tiles.md section 4.2:
   * 3 red, 4 green, 6 for S-57 COLOUR 1, 6, 9 and 11).
   */
  private static final Map<String, String> SECTOR_GLOW = Map.of(
    "red", "3", "green", "4", "white", "6", "yellow", "6", "amber", "6", "orange", "6");
  /** Seamark types an object passes under, whose vertical clearances are written in metres. */
  private static final Set<String> OVERHEAD = Set.of("bridge", "cable_overhead", "pipeline_overhead");
  /**
   * {@code seamark:<type>:<key>} (S-57 VERCLR, VERCSA, VERCCL, VERCOP) and the field each is written
   * to as metres.
   */
  private static final Map<String, String> CLEARANCES = Map.of(
    "clearance_height", "dh_clearance", "clearance_height_safe", "dh_clearance_safe",
    "clearance_height_closed", "dh_clearance_closed", "clearance_height_open", "dh_clearance_open");
  /** Metres: a plain decimal, optionally with {@code m}. Not {@code M}: that is nautical miles. */
  private static final Pattern METRES = Pattern.compile("^(\\d+(?:\\.\\d+)?|\\.\\d+)\\s*m?$");
  /** Feet as OSM writes them: {@code 15'}, {@code 15 ft} (any case) or with inches, {@code 15'6"}. */
  private static final Pattern FEET =
    Pattern.compile("^(\\d+(?:\\.\\d+)?|\\.\\d+)\\s*(?:'|(?i:ft))(?:\\s*(\\d+(?:\\.\\d+)?|\\.\\d+)\\s*\")?$");
  private static final BigDecimal FOOT = new BigDecimal("0.3048");
  private static final BigDecimal INCH = new BigDecimal("0.0254");
  private static final BigDecimal INCHES_PER_FOOT = BigDecimal.valueOf(12);

  private final String osmUrl;
  private final String landUrl;
  private final AtomicLong landSeen = new AtomicLong();
  private final AtomicLong landEmitted = new AtomicLong();

  SeamapCompiler(String osmUrl, String landUrl) {
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

  private static String chineseName(SourceFeature source) {
    for (String key : List.of("name:zh-Hans", "name:zh", "name")) {
      String name = value(source, key);
      if (name != null && !name.isBlank()) {
        return name;
      }
    }
    return null;
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
    // A light belongs to its seamark however it is mapped: Fan Lau lighthouse (way 822828466) tags
    // its sector light on the building outline.
    String[] light = lightFields(source.tags());
    feature.setAttr("type", type)
      .setAttr("category", category != null ? category : value(source, prefix + "category"))
      .setAttr("shape", value(source, prefix + "shape"))
      .setAttr("color", colours(value(source, prefix + "colour")))
      .setAttr("color_pattern", value(source, prefix + "colour_pattern"))
      .setAttr("topmark_shape", value(source, "seamark:topmark:shape"))
      .setAttr("topmark_color", colours(value(source, "seamark:topmark:colour")))
      .setAttr("light_color", colours(value(source, "seamark:light:colour")))
      .setAttr("family", "light_major".equals(type) ? "major_aid" : null)
      .setAttr("dh_sectors", light == null ? null : light[0])
      .setAttr("dh_light_colour", light == null ? null : light[1])
      .setAttr("dh_allround_colour", light == null ? null : light[2])
      .setZoomRange(MIN_ZOOM, MAX_ZOOM)
      .setMinPixelSizeAtAllZooms(0)
      .setBufferPixels(16);
    if (OVERHEAD.contains(type)) {
      CLEARANCES.forEach((key, field) -> feature.setAttr(field, clearanceMetres(source.getTag(prefix + key))));
    }
    return feature;
  }

  /**
   * An OSM vertical clearance in metres, rounded down to the centimetre, or {@code null} when it is
   * not one of the forms below or not above 0 once rounded: a clearance is never guessed or rounded up.
   * A plain decimal is metres, also with {@code m}; feet are {@code 15'}, {@code 15 ft} (any case)
   * or {@code 15'6"} (inches below 12), 0.3048 m each.
   */
  static Double clearanceMetres(Object value) {
    if (value == null) {
      return null;
    }
    String text = value.toString().trim();
    BigDecimal metres;
    Matcher matcher;
    if ((matcher = METRES.matcher(text)).matches()) {
      metres = new BigDecimal(matcher.group(1));
    } else if ((matcher = FEET.matcher(text)).matches()) {
      BigDecimal inches = matcher.group(2) == null ? BigDecimal.ZERO : new BigDecimal(matcher.group(2));
      if (inches.compareTo(INCHES_PER_FOOT) >= 0) {
        return null;
      }
      metres = new BigDecimal(matcher.group(1)).multiply(FOOT).add(inches.multiply(INCH));
    } else {
      return null;
    }
    metres = metres.setScale(2, RoundingMode.FLOOR);
    return metres.signum() > 0 ? metres.doubleValue() : null;
  }

  /**
   * The lights of one seamark as {@code {dh_sectors, dh_light_colour, dh_allround_colour}} in the
   * encoding of DingHai's S-57 light stations (dinghai-legal chartset/tiles.md section 4.2), each
   * {@code null} when not written; {@code null} when none is.
   *
   * <p>Lights are the unnumbered {@code seamark:light:*} and each {@code seamark:light:N:*}, in
   * that order and by ascending N. One is a sector when {@code sector_start} and {@code sector_end}
   * (bearings from seaward, clockwise, as S-57 SECTR1 and SECTR2) are distinct decimal degrees in
   * 0-360 (360 is written 0), else a direction when its {@code category} lists {@code directional}
   * or {@code moire} (CATLIT 1 or 16) with an {@code orientation} in 0-360 and it is not obscured;
   * any other light is not in the rose, as the App's own resolver of S-57 lights decides.
   * Degrees are written as tagged, without trailing zeros. {@code dh_sectors} lists the sectors and
   * directions when there is one; an OSM seamark is a whole station, so it is never {@code hub}.
   *
   * <p>{@code dh_light_colour} is the station's overview-dot and lamp colour: the one glow colour
   * shared by every light that is not obscured (each numbered light, all-round ones included, and
   * an unnumbered light in the rose), else {@code multi}. It is written for a seamark with numbered
   * lights or a rose, unless the unnumbered {@code seamark:light:colour} (its {@code light_color})
   * already colours it.
   *
   * <p>{@code dh_allround_colour} is the glow of the all-round lights beside a rose, which S-52
   * draws with their own lamp and glow: the one glow colour shared by every all-round light that
   * is not obscured, else {@code multi}. An all-round light is a numbered light that is neither a
   * sector nor a direction, or the unnumbered light when it is neither and its
   * {@code seamark:light:colour} is tagged. It is written only with {@code dh_sectors} and when there
   * is such a light.
   */
  static String[] lightFields(Map<String, Object> tags) {
    // By value, then as written: "02" and "2" are distinct keys.
    TreeSet<String> numbers = new TreeSet<>(
      Comparator.comparing((String number) -> new BigInteger(number)).thenComparing(Comparator.naturalOrder()));
    for (String key : tags.keySet()) {
      Matcher matcher = NUMBERED_LIGHT.matcher(key);
      if (matcher.find()) {
        numbers.add(matcher.group(1));
      }
    }
    List<String> prefixes = new ArrayList<>();
    prefixes.add("seamark:light:");
    numbers.forEach(number -> prefixes.add("seamark:light:" + number + ":"));

    List<String> entries = new ArrayList<>();
    Set<String> dotColours = new TreeSet<>();
    Set<String> allRoundColours = new TreeSet<>();
    boolean unmapped = false;
    boolean allRoundUnmapped = false;
    boolean allRoundLight = false;
    for (String prefix : prefixes) {
      Set<String> visibility = words(tags.get(prefix + "visibility"));
      String shown = visibility.contains("obscured") ? "o"
        : visibility.contains("faint") || visibility.contains("part_obscured") ? "f" : "l";
      List<String> colours = "o".equals(shown) ? List.of() : glowColours(tags.get(prefix + "colour"));
      String start = degrees(tags.get(prefix + "sector_start"));
      String end = degrees(tags.get(prefix + "sector_end"));
      String orientation = degrees(tags.get(prefix + "orientation"));
      Set<String> category = words(tags.get(prefix + "category"));
      if (start != null && end != null && !start.equals(end)) {
        entries.add("s:" + start + "," + end + "," + shown + "," + String.join("+", colours));
      } else if (!"o".equals(shown) && orientation != null &&
        (category.contains("directional") || category.contains("moire"))) {
        entries.add("d:" + orientation + "," + shown + "," + String.join("+", colours));
      } else {
        boolean unnumbered = prefix.equals("seamark:light:");
        // An all-round light. The unnumbered one is a light only with a colour: alone, its range,
        // height or reference describe the numbered lights. It never colours the station: its
        // colour is light_color, which then leaves dh_light_colour unwritten.
        if (!"o".equals(shown) && (!unnumbered || tags.get(prefix + "colour") != null)) {
          allRoundLight = true;
          allRoundUnmapped |= colours.isEmpty();
          allRoundColours.addAll(colours);
        }
        if (unnumbered) {
          continue;
        }
      }
      if (!"o".equals(shown)) {
        unmapped |= colours.isEmpty();
        dotColours.addAll(colours);
      }
    }
    String sectors = entries.isEmpty() ? null : String.join(";", entries);
    String dot = tags.get("seamark:light:colour") != null || (numbers.isEmpty() && sectors == null) ? null
      : oneGlow(dotColours, unmapped);
    String allRound = sectors == null || !allRoundLight ? null
      : oneGlow(allRoundColours, allRoundUnmapped);
    if (sectors == null && dot == null) {
      return null;
    }
    return new String[]{sectors, dot, allRound};
  }

  /** The one glow colour of some lights, else {@code multi} (magenta). */
  private static String oneGlow(Set<String> glows, boolean unmapped) {
    return !unmapped && glows.size() == 1 ? glows.iterator().next() : "multi";
  }

  /** Lower-case values of an OSM list ({@code a;b}). */
  private static Set<String> words(Object value) {
    Set<String> words = new TreeSet<>();
    if (value != null) {
      for (String word : value.toString().split(";")) {
        words.add(word.trim().toLowerCase());
      }
    }
    return words;
  }

  /**
   * Glow codes of an OSM light colour, as the App reads S-57 COLOUR for a sector: repeats of one
   * glow merged; none when a colour has no glow, when more than two remain, or when untagged.
   */
  private static List<String> glowColours(Object value) {
    List<String> glows = new ArrayList<>();
    if (value == null) {
      return glows;
    }
    for (String colour : value.toString().split(";", -1)) {
      String glow = SECTOR_GLOW.get(colour.trim().toLowerCase());
      if (glow == null) {
        return List.of();
      }
      if (glows.isEmpty() || !glows.get(glows.size() - 1).equals(glow)) {
        glows.add(glow);
      }
    }
    return glows.size() <= 2 ? glows : List.of();
  }

  /** Decimal degrees in 0-360 as written (360 as 0), or {@code null}. */
  private static String degrees(Object value) {
    if (value == null || !DEGREES.matcher(value.toString().trim()).matches()) {
      return null;
    }
    BigDecimal degrees = new BigDecimal(value.toString().trim());
    if (degrees.compareTo(BigDecimal.valueOf(360)) > 0) {
      return null;
    }
    return degrees.compareTo(BigDecimal.valueOf(360)) == 0 ? "0" : degrees.stripTrailingZeros().toPlainString();
  }

  private static String landuse(SourceFeature source) {
    for (String key : List.of("landuse", "leisure", "natural")) {
      String kind = LANDUSE.get(key + "=" + value(source, key));
      if (kind != null) {
        return kind;
      }
    }
    return null;
  }

  @Override
  public void processFeature(SourceFeature source, FeatureCollector features) {
    if ("land_polygons".equals(source.getSource())) {
      landSeen.incrementAndGet();
      if (source.canBePolygon()) {
        landEmitted.incrementAndGet();
        features.polygon("land").setZoomRange(0, MAX_ZOOM)
          .setPixelToleranceBelowZoom(MIN_ZOOM - 1, OVERVIEW_PIXEL_TOLERANCE);
      }
      return;
    }
    if ("depth".equals(source.getSource())) {
      // depth_product.py already generalised each zoom on its own lattice and dropped fragments.
      int zoom = ((Number) source.getTag("zoom")).intValue();
      features.polygon("depth").setZoomRange(zoom, zoom)
        .setAttr("min", ((Number) source.getTag("min")).intValue())
        .setPixelToleranceAtAllZooms(0).setMinPixelSizeAtAllZooms(0);
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
      // The overview fills lakes and reservoirs, as GSHHG did, not river polygons.
      boolean lake = !"riverbank".equals(waterway) && (water == null || !RIVER_WATER.contains(water));
      features.polygon("water").setZoomRange(lake ? 0 : MIN_ZOOM, MAX_ZOOM)
        .setPixelToleranceBelowZoom(MIN_ZOOM - 1, OVERVIEW_PIXEL_TOLERANCE)
        .setMinPixelSizeBelowZoom(MIN_ZOOM - 1, OVERVIEW_MIN_LAKE_PIXELS)
        .inheritAttrsFromSourceWithMinzoom(MIN_ZOOM, "name", "water");
    }

    String wetland = value(source, "wetland");
    if (source.canBePolygon() && "wetland".equals(natural) && wetland != null && COASTAL_WETLAND.contains(wetland)) {
      Feature feature = features.polygon("wetland").setZoomRange(MIN_ZOOM, MAX_ZOOM)
        .setAttr("type", "wetland").setAttr("category", wetland).setMinPixelSizeAtAllZooms(0);
      feature.inheritAttrsFromSource("name", "surface");
    }

    String place = value(source, "place");
    if ("sea".equals(place) || "bay".equals(natural)) {
      Feature feature = (source.canBePolygon() ? features.pointOnSurface("sea_area") : features.anyGeometry("sea_area"))
        .setZoomRange(MIN_ZOOM, MAX_ZOOM).setAttr("category", "bay".equals(natural) ? "bay" : "sea");
      feature.inheritAttrsFromSource("name", "name:zh", "name:zh-Hans");
    }

    String landClass = source.canBePolygon() ? landuse(source) : null;
    if (landClass != null) {
      features.polygon("landuse").setAttr("class", landClass).setZoomRange(MIN_ZOOM, LAND_MAX_ZOOM)
        .setMinPixelSize(2).setPixelTolerance(0.125).setPixelToleranceBelowZoom(LAND_MAX_ZOOM - 1, 0.25);
    }

    String highway = value(source, "highway");
    String road = highway != null && source.canBeLine() ? ROADS.get(highway) : null;
    if (road != null) {
      features.line("roads").setAttr("class", road)
        .setZoomRange("secondary".equals(road) ? 11 : MIN_ZOOM, LAND_MAX_ZOOM);
    }

    if (source.isPoint() && "peak".equals(natural)) {
      Double ele = Parse.meters(source.getTag("ele"));
      if (ele != null && Double.isFinite(ele)) {
        features.point("peaks").setAttr("ele", (int) Math.round(ele)).setZoomRange(11, LAND_MAX_ZOOM)
          .setBufferPixels(0);
      }
    }

    String name = chineseName(source);
    if (name != null && "island".equals(place) && source.canBePolygon()) {
      features.innermostPoint("places").setAttr("kind", "island").setAttr("name", name)
        .setZoomRange(MIN_ZOOM, LAND_MAX_ZOOM).setMinPixelSize(20).setBufferPixels(0);
    } else if (name != null && place != null && source.isPoint() && PLACES.containsKey(place)) {
      features.point("places").setAttr("kind", place).setAttr("name", name)
        .setZoomRange(PLACES.get(place), LAND_MAX_ZOOM).setBufferPixels(0);
    }
  }

  @Override
  public List<VectorTile.Feature> postProcessLayerFeatures(String layer, int zoom, List<VectorTile.Feature> items) {
    // One line per road class per tile, as before; short gaps stay and nothing is dropped.
    return "roads".equals(layer) ? FeatureMerge.mergeLineStrings(items, 0, zoom < LAND_MAX_ZOOM ? 0.25 : 0.125, 4) :
      items;
  }

  /** Tile-decoded polygons can self-touch after rounding; overlay rejects them (pilot tile 10/837/446). */
  private static Geometry valid(Geometry geometry) {
    return geometry.isValid() ? geometry : GeometryFixer.fix(geometry);
  }

  /** Land use only on land: merged by class, then clipped to the tile's land polygons. */
  @Override
  public Map<String, List<VectorTile.Feature>> postProcessTileFeatures(TileCoord tile,
    Map<String, List<VectorTile.Feature>> layers) throws GeometryException {
    List<VectorTile.Feature> landuse = layers.get("landuse");
    if (landuse == null) {
      return layers;
    }
    Map<String, List<VectorTile.Feature>> result = new HashMap<>(layers);
    List<VectorTile.Feature> clipped = new ArrayList<>();
    List<Geometry> parts = new ArrayList<>();
    for (var feature : layers.getOrDefault("land", List.of())) {
      parts.add(valid(feature.geometry().decode()));
    }
    // UnaryUnion returns null when the tile's land decodes to nothing.
    Geometry land = parts.isEmpty() ? null : OverlayNGRobust.union(parts);
    if (land != null) {
      for (var feature : FeatureMerge.mergeOverlappingPolygons(landuse, 1)) {
        Geometry inside = OverlayNGRobust.overlay(valid(feature.geometry().decode()), land, OverlayNG.INTERSECTION);
        List<Polygon> polygons = new ArrayList<>();
        for (int i = 0; i < inside.getNumGeometries(); i++) {
          if (inside.getGeometryN(i) instanceof Polygon polygon && polygon.getArea() >= 1) {
            polygons.add(polygon);
          }
        }
        if (!polygons.isEmpty()) {
          clipped.add(feature.copyWithNewGeometry(GeoUtils.createMultiPolygon(polygons)));
        }
      }
    }
    if (clipped.isEmpty()) {
      result.remove("landuse");
    } else {
      result.put("landuse", clipped);
    }
    return result;
  }

  @Override public String name() { return "DingHai Seamap OSM"; }
  @Override public String description() { return "DingHai Seamap layers compiled from OpenStreetMap and GEBCO"; }
  @Override public boolean isOverlay() { return true; }
  @Override public String attribution() { return OSM_ATTRIBUTION; }
  @Override public String version() { return "seamap-osm-2"; }
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
      "dinghai:generator", "seamap-osm-2",
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
    Path depth = Path.of(args.getString("depth", "depth_product.py output directory", "depth"));
    Path output = Path.of(args.getString("output", "output PMTiles", "seamap.pmtiles"));
    String osmUrl = args.getString("osm_url", "OSM source URL", "https://planet.openstreetmap.org/pbf/planet-latest.osm.pbf");
    String landUrl = args.getString("land_url", "land polygon source URL",
      "https://osmdata.openstreetmap.de/download/land-polygons-complete-4326.zip");
    Planetiler.create(args)
      .setProfile(new SeamapCompiler(osmUrl, landUrl))
      .addOsmSource("osm", osm, osmUrl)
      .addShapefileSource("EPSG:4326", "land_polygons", land, landUrl)
      .addShapefileGlobSource("EPSG:4326", "depth", depth, "x*.shp", null)
      .overwriteOutput(output)
      .run();
  }
}
