// SPDX-License-Identifier: LGPL-3.0-only
package app.dinghai;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;

import java.util.HashMap;
import java.util.Map;
import org.junit.jupiter.api.Test;

/**
 * Protects the cross-repository contract that the App decodes with the S-57 rose decoder
 * (dinghai-legal chartset/tiles.md section 4.2): which OSM lights enter {@code dh_sectors}, how
 * visibility, colour and degrees are written, and which seamarks get the station colour
 * {@code dh_light_colour} the App draws lights without {@code light_color} in.
 */
class LightFieldsTest {
  private static Map<String, Object> tags(String... pairs) {
    Map<String, Object> tags = new HashMap<>();
    for (int i = 0; i < pairs.length; i += 2) {
      tags.put(pairs[i], pairs[i + 1]);
    }
    return tags;
  }

  @Test
  void osmLightTagsEncodeAsTheS57LightStationFields() {
    Object[][] cases = {
      // The OSM wiki's sectored light: green, white, red Fl(2) 10s sectors.
      {tags("seamark:type", "light_major",
        "seamark:light:1:colour", "green", "seamark:light:1:sector_start", "230", "seamark:light:1:sector_end", "260",
        "seamark:light:2:colour", "white", "seamark:light:2:sector_start", "260", "seamark:light:2:sector_end", "280",
        "seamark:light:3:colour", "red", "seamark:light:3:sector_start", "280", "seamark:light:3:sector_end", "310"),
        new String[]{"s:230,260,l,4;s:260,280,l,6;s:280,310,l,3", "multi"}},
      // The OSM wiki's directional light; alternating colours keep their order.
      {tags("seamark:light:1:category", "directional", "seamark:light:1:colour", "green",
        "seamark:light:1:orientation", "263",
        "seamark:light:2:category", "directional", "seamark:light:2:colour", "green;white",
        "seamark:light:2:orientation", "267",
        "seamark:light:3:category", "moire", "seamark:light:3:colour", "red", "seamark:light:3:orientation", "277"),
        new String[]{"d:263,l,4;d:267,l,4+6;d:277,l,3", "multi"}},
      // One lit sector and the obscured rest: the dot takes the lit colour.
      {tags("seamark:light:1:colour", "white", "seamark:light:1:sector_start", "272",
        "seamark:light:1:sector_end", "245",
        "seamark:light:2:visibility", "obscured", "seamark:light:2:colour", "white",
        "seamark:light:2:sector_start", "245", "seamark:light:2:sector_end", "272"),
        new String[]{"s:272,245,l,6;s:245,272,o,", "6"}},
      // Faint and partly obscured are faint; untagged or unglowing colours draw magenta.
      {tags("seamark:light:1:visibility", "faint", "seamark:light:1:colour", "red",
        "seamark:light:1:sector_start", "10", "seamark:light:1:sector_end", "20",
        "seamark:light:2:visibility", "part_obscured", "seamark:light:2:colour", "red",
        "seamark:light:2:sector_start", "20", "seamark:light:2:sector_end", "30",
        "seamark:light:3:colour", "blue", "seamark:light:3:sector_start", "30", "seamark:light:3:sector_end", "40"),
        new String[]{"s:10,20,f,3;s:20,30,f,3;s:30,40,l,", "multi"}},
      // Numeric order of N; degrees as tagged without trailing zeros, 360 as 0; the unnumbered light first.
      // Its seamark:light:colour is light_color already: no dh_light_colour.
      {tags("seamark:light:colour", "yellow", "seamark:light:sector_start", "045.50", "seamark:light:sector_end", "90",
        "seamark:light:10:colour", "yellow", "seamark:light:10:sector_start", "300", "seamark:light:10:sector_end", "360.0",
        "seamark:light:2:colour", "amber", "seamark:light:2:sector_start", "90", "seamark:light:2:sector_end", "300"),
        new String[]{"s:45.5,90,l,6;s:90,300,l,6;s:300,0,l,6", null}},
      // Malformed or out-of-range bearings, 0-360 (all round) and an obscured direction are not in the
      // rose; the lights still colour the station.
      {tags("seamark:light:1:colour", "red", "seamark:light:1:sector_start", "245d", "seamark:light:1:sector_end", "10",
        "seamark:light:2:colour", "red", "seamark:light:2:sector_start", "0", "seamark:light:2:sector_end", "360",
        "seamark:light:3:colour", "red", "seamark:light:3:sector_start", "10", "seamark:light:3:sector_end", "400",
        "seamark:light:4:category", "directional", "seamark:light:4:orientation", "90",
        "seamark:light:4:visibility", "obscured"),
        new String[]{null, "3"}},
      // A sector and a numbered all-round light (OSM node 1136085219): both colour the station; the
      // unnumbered reference is not a light.
      {tags("seamark:light:reference", "P 3572",
        "seamark:light:1:colour", "white", "seamark:light:1:sector_start", "156.5", "seamark:light:1:sector_end", "337",
        "seamark:light:2:colour", "red", "seamark:light:2:character", "Fl", "seamark:light:2:range", "12"),
        new String[]{"s:156.5,337,l,6", "multi"}},
      // Only numbered all-round lights: a station colour, no rose.
      {tags("seamark:light:range", "5", "seamark:light:1:colour", "green", "seamark:light:1:character", "Fl"),
        new String[]{null, "4"}},
      // An unnumbered sector without a colour: magenta.
      {tags("seamark:light:sector_start", "10", "seamark:light:sector_end", "20"), new String[]{"s:10,20,l,", "multi"}},
      // light_color already colours an unnumbered all-round light, with or without numbered ones.
      {tags("seamark:light:colour", "white", "seamark:light:1:colour", "red", "seamark:light:1:character", "Fl"), null},
      {tags("seamark:light:colour", "white", "seamark:light:character", "Fl"), null},
      {tags("seamark:type", "buoy_lateral"), null},
    };
    for (Object[] row : cases) {
      @SuppressWarnings("unchecked") Map<String, Object> input = (Map<String, Object>) row[0];
      assertArrayEquals((String[]) row[1], SeamapCompiler.lightFields(input), input.toString());
    }
  }
}
