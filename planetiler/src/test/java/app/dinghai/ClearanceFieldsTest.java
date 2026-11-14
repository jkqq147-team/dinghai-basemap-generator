// SPDX-License-Identifier: LGPL-3.0-only
package app.dinghai;

import static org.junit.jupiter.api.Assertions.assertEquals;

import com.onthegomap.planetiler.FeatureCollector;
import com.onthegomap.planetiler.config.PlanetilerConfig;
import com.onthegomap.planetiler.geo.GeoUtils;
import com.onthegomap.planetiler.reader.SimpleFeature;
import com.onthegomap.planetiler.stats.Stats;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.locationtech.jts.geom.Coordinate;

/**
 * Protects the metre clearance fields the App draws and reads for mast height instead of parsing
 * the OSM strings: a clearance must never come out larger than tagged, so feet are converted and
 * everything rounds down; anything else is left unwritten rather than guessed.
 */
class ClearanceFieldsTest {
  @Test
  void osmClearancesParseToMetresRoundedDown() {
    Object[][] cases = {
      // Metres, plain or with m.
      {"17", 17.0}, {"54.6", 54.6}, {" 8 ", 8.0}, {"5m", 5.0}, {"5.25 m", 5.25}, {".5", 0.5},
      {"12.349", 12.34}, {"12.999", 12.99},
      // Feet: 15' is 4.572 m, not 15 m.
      {"15'", 4.57}, {"15 ft", 4.57}, {"15ft", 4.57}, {"45 FT", 13.71}, {"15'6\"", 4.72}, {"15' 6\"", 4.72},
      {"12.5'", 3.81}, {"0'11\"", 0.27},
      // Unparseable: never the leading number.
      {"15 feet", null}, {"5 M", null}, {"4,5", null}, {"10;12", null}, {"1m30", null}, {"approx 8", null},
      {"8m approx", null}, {"15'12\"", null}, {"6\"", null}, {"1e2", null}, {"unknown", null}, {"", null},
      {null, null},
      // Not above 0 once rounded.
      {"0", null}, {"0.009", null}, {"-5", null}, {"0'0\"", null},
    };
    for (Object[] row : cases) {
      assertEquals(row[1], SeamapCompiler.clearanceMetres(row[0]), String.valueOf(row[0]));
    }
  }

  /**
   * Overhead cables and pipelines are mapped as ways (OSM wiki: onWay only); they and bridges carry
   * every clearance as a metre field in the seamark layer.
   */
  @Test
  void overheadSeamarksCarryTheirClearancesInMetres() {
    var line = GeoUtils.JTS_FACTORY.createLineString(new Coordinate[]{
      new Coordinate(113.9280, 22.2880), new Coordinate(113.9300, 22.2890)});
    var point = GeoUtils.JTS_FACTORY.createPoint(new Coordinate(114.0729542, 22.350956));
    Object[][] cases = {
      {line, Map.of("seamark:type", "cable_overhead", "seamark:cable_overhead:clearance_height_safe", "12",
        "seamark:cable_overhead:clearance_height", "40'"),
        "LineString", Map.of("dh_clearance_safe", 12.0, "dh_clearance", 12.19)},
      {line, Map.of("seamark:type", "pipeline_overhead", "seamark:pipeline_overhead:clearance_height", "7.5 m"),
        "LineString", Map.of("dh_clearance", 7.5)},
      {point, Map.of("seamark:type", "bridge", "seamark:bridge:category", "lifting",
        "seamark:bridge:clearance_height_closed", "15'6\"", "seamark:bridge:clearance_height_open", "30",
        "seamark:bridge:clearance_height", "unknown"),
        "Point", Map.of("dh_clearance_closed", 4.72, "dh_clearance_open", 30.0)},
      // Another type's clearance is not read: only objects passed under.
      {point, Map.of("seamark:type", "gate", "seamark:gate:clearance_height", "5"), "Point", Map.of()},
    };
    long id = 1;
    for (Object[] row : cases) {
      @SuppressWarnings("unchecked") Map<String, Object> tags = new HashMap<>((Map<String, Object>) row[1]);
      var source = SimpleFeature.create((org.locationtech.jts.geom.Geometry) row[0], tags, "osm", null, id++);
      var features = new FeatureCollector.Factory(PlanetilerConfig.defaults(), Stats.inMemory()).get(source);
      new SeamapCompiler("osm", "land").processFeature(source, features);
      List<String> geometries = new ArrayList<>();
      Map<String, Object> clearances = new HashMap<>();
      features.forEach(feature -> {
        if ("seamark".equals(feature.getLayer())) {
          geometries.add(feature.getGeometry().getGeometryType());
          feature.getAttrsAtZoom(SeamapCompiler.MAX_ZOOM).forEach((key, value) -> {
            if (key.startsWith("dh_clearance")) {
              clearances.put(key, value);
            }
          });
        }
      });
      assertEquals(List.of(row[2]), geometries, tags.toString());
      assertEquals(row[3], clearances, tags.toString());
    }
  }
}
