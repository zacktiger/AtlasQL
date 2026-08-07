// The result globe.
//
// Results are drawn on the actual planet rather than in a table alone, because
// the answer to "which countries are these" is a shape, not a list of names.
// The projection is orthographic: the map *is* a globe at every zoom level, so
// zooming out from one country to the whole world is one continuous gesture
// rather than a switch between two different maps.
//
// Three things here are worth knowing before changing anything:
//
//   * The basemap is our own regions table, not a tile service. Every coastline
//     on screen is a boundary some query could have returned, so a shape never
//     disagrees with a row in the table.
//   * Orthographic projects an angular distance t from the centre of the disc
//     to a pixel radius scale*sin(t). Every fit and zoom calculation below is
//     that one identity rearranged; nothing is fudged by trial and error.
//   * Colour comes from CSS custom properties, read off the canvas, so
//     styles.css stays the only place light and dark are described. The ramp is
//     sequential single-hue: it encodes magnitude, and its light end is allowed
//     to recede into the surface.

const RAMP_STEPS = 7;
const TAU = Math.PI * 2;

// Below this the animation is a jump, above it a wander. 700ms reads as "the
// globe turned to look at that", which is the point of the gesture.
const FLY_MS = 700;

// Padding factor when fitting: results should not touch the edge of the canvas.
const FIT_PADDING = 1.35;

// A single city is a point with no extent, so without a floor the fit would
// divide by zero and zoom to infinity.
//
// The floor is set by the basemap, not by taste. The whole world is served
// simplified — a quarter degree at rest, a twentieth once the zoom asks for it —
// and a simplification only looks like a coastline while it stays small against
// the visible span. At 0.06 radians (~3.4 degrees, roughly a 400 km radius) a
// 0.05 degree segment is about one percent of the view; closer than that and the
// land dissolves into spikes and triangles, which reads as a rendering bug
// rather than as the edge of the data. Zooming in further by hand is still
// allowed — that is the user choosing to look at the simplification. Flying
// there on their behalf and presenting it as the answer is not.
const MIN_FIT_RADIANS = 0.06;

// 80 degrees from the centre of the disc. Past this a region is so foreshortened
// by the curve of the globe that calling it visible would be a lie.
const VISIBLE_LIMIT = (80 * Math.PI) / 180;

// Interaction level of detail.
//
// Redrawing the world basemap is essentially the entire cost of a frame: 57 ms
// against 0.2 ms for the ocean disc and 2 ms for the graticule, which is 17 fps
// while dragging. The cost is projecting every vertex, so neither d3's
// resampling precision nor a coarser server-side tolerance moves it much —
// ST_SimplifyPreserveTopology refuses to collapse a polygon out of existence, so
// even at the maximum tolerance the count only falls from 30,087 vertices to
// 22,425. Almost all of those vertices are in specks: 258 countries, but
// thousands of tiny islands.
//
// So the draft drops rings below half a degree across rather than simplifying
// them further, which is the one thing that actually works: 30,087 vertices to
// 12,511, and 57 ms to 16 ms — 61 fps. It is drawn only while the camera is
// moving, and the full basemap comes back once it settles. What you lose during
// a drag is islands smaller than the gap between two graticule lines, in motion.
const DRAFT_MIN_SPAN_DEGREES = 0.5;

// Long enough that a pause between two drag gestures does not flash the full
// basemap, short enough to feel like the map sharpens as you let go.
const SETTLE_MS = 120;

function clamp(value, low, high) {
  return Math.min(high, Math.max(low, value));
}

function easeInOut(t) {
  return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
}

// Rotating from 350° to 10° should cross zero, not travel 340° the long way.
function shortestDelta(from, to) {
  return ((((to - from) % 360) + 540) % 360) - 180;
}

export function createGlobe(canvas, { onSelect, onHover, onCamera } = {}) {
  const ctx = canvas.getContext("2d");
  const projection = window.d3.geoOrthographic().clipAngle(90).precision(0.4);
  const path = window.d3.geoPath(projection, ctx);
  const graticule = window.d3.geoGraticule10();

  let basemap = null;
  let basemapDraft = null;
  let interacting = false;
  let settleTimer = null;
  let results = [];
  let values = new Map(); // region_id -> numeric value driving the ramp
  let domain = null; // [min, max] of those values
  let selectedId = null;
  let hoveredId = null;

  let rotation = [0, -15];
  let scale = 1;
  let minScale = 1;
  let width = 0;
  let height = 0;
  let palette = readPalette();
  let animation = null;
  let frame = null;

  function readPalette() {
    const style = getComputedStyle(canvas);
    const read = (name) => style.getPropertyValue(name).trim();
    return {
      ocean: read("--map-ocean"),
      land: read("--map-land"),
      landLine: read("--map-land-line"),
      graticule: read("--map-graticule"),
      surface: read("--map-surface"),
      select: read("--map-select"),
      ink: read("--map-ink"),
      ramp: Array.from({ length: RAMP_STEPS }, (_, i) => read(`--ramp-${i}`)),
    };
  }

  // Sequential encoding: one hue, light to dark, linear in the value. Linear
  // rather than by rank on purpose - if the top ten are all within a percent of
  // each other, the map should show that they are, not manufacture a spread.
  function colorFor(regionId) {
    const value = values.get(regionId);
    if (value === undefined || !domain) return palette.ramp[Math.floor(RAMP_STEPS / 2)];
    const [low, high] = domain;
    const t = high > low ? (value - low) / (high - low) : 1;
    return palette.ramp[clamp(Math.round(t * (RAMP_STEPS - 1)), 0, RAMP_STEPS - 1)];
  }

  // The widest span of a ring's bounding box, in degrees. Cheap, and enough to
  // tell an island worth drawing at speed from a speck that is not.
  function ringSpan(ring) {
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const [x, y] of ring) {
      if (x < minX) minX = x;
      if (x > maxX) maxX = x;
      if (y < minY) minY = y;
      if (y > maxY) maxY = y;
    }
    return Math.max(maxX - minX, maxY - minY);
  }

  // A version of the basemap cheap enough to redraw at 60 fps, for frames where
  // the camera is moving. Rings smaller than DRAFT_MIN_SPAN_DEGREES are dropped
  // outright — including interior rings, so a lake or an enclave fills in for
  // the length of a drag. A feature left with no ring at all is dropped too.
  function buildDraft(collection) {
    const features = [];
    for (const feature of collection.features ?? []) {
      const geometry = feature.geometry;
      if (!geometry) continue;
      const polygons =
        geometry.type === "Polygon"
          ? [geometry.coordinates]
          : geometry.type === "MultiPolygon"
            ? geometry.coordinates
            : null;
      if (!polygons) continue;

      const kept = [];
      for (const rings of polygons) {
        if (ringSpan(rings[0]) < DRAFT_MIN_SPAN_DEGREES) continue;
        kept.push(rings.filter((ring, i) => i === 0 || ringSpan(ring) >= DRAFT_MIN_SPAN_DEGREES));
      }
      if (kept.length) {
        features.push({
          type: "Feature",
          properties: feature.properties,
          geometry: { type: "MultiPolygon", coordinates: kept },
        });
      }
    }
    return { type: "FeatureCollection", features };
  }

  // Any camera movement: a drag, a wheel, or a fly. Draws draft frames until
  // things stop, then one full-detail frame.
  function beginInteraction() {
    interacting = true;
    if (settleTimer) {
      clearTimeout(settleTimer);
      settleTimer = null;
    }
  }

  function endInteraction() {
    if (settleTimer) clearTimeout(settleTimer);
    settleTimer = setTimeout(() => {
      settleTimer = null;
      interacting = false;
      draw();
    }, SETTLE_MS);
  }

  function resize() {
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const dpr = window.devicePixelRatio || 1;
    width = rect.width;
    height = rect.height;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const wasAtMinimum = scale <= minScale + 0.001;
    minScale = (Math.min(width, height) / 2) * 0.92;
    scale = wasAtMinimum ? minScale : Math.max(scale, minScale);
    projection.translate([width / 2, height / 2]);
    draw();
  }

  function draw() {
    if (frame) return;
    frame = requestAnimationFrame(() => {
      frame = null;
      render();
    });
  }

  function render() {
    if (!width || !height) return;
    projection.rotate([rotation[0], rotation[1]]).scale(scale);
    ctx.clearRect(0, 0, width, height);

    // Ocean disc. Also the thing that makes the sphere read as a sphere when
    // zoomed out far enough to see its edge.
    ctx.beginPath();
    path({ type: "Sphere" });
    ctx.fillStyle = palette.ocean;
    ctx.fill();

    ctx.beginPath();
    path(graticule);
    ctx.strokeStyle = palette.graticule;
    ctx.lineWidth = 0.5;
    ctx.stroke();

    const land = interacting && basemapDraft ? basemapDraft : basemap;
    if (land) {
      ctx.beginPath();
      path(land);
      ctx.fillStyle = palette.land;
      ctx.fill();
      ctx.strokeStyle = palette.landLine;
      ctx.lineWidth = 0.5;
      ctx.stroke();
    }

    // Polygons first, then points, so a city marker is never buried under the
    // country it sits in.
    for (const feature of results) {
      if (feature.properties.kind !== "polygon" || !feature.geometry) continue;
      ctx.beginPath();
      path(feature);
      ctx.fillStyle = colorFor(feature.properties.region_id);
      ctx.fill();
      // A hairline in the surface colour, so two adjacent result regions read
      // as two regions rather than one merged blob.
      ctx.strokeStyle = palette.surface;
      ctx.lineWidth = 1;
      ctx.stroke();
    }

    for (const feature of results) {
      if (feature.properties.kind === "polygon" || !feature.geometry) continue;
      const point = projectVisible(feature.geometry.coordinates);
      if (!point) continue;
      ctx.beginPath();
      ctx.arc(point[0], point[1], 5, 0, TAU);
      ctx.fillStyle = colorFor(feature.properties.region_id);
      ctx.fill();
      ctx.strokeStyle = palette.surface;
      ctx.lineWidth = 2;
      ctx.stroke();
    }

    for (const id of [hoveredId, selectedId]) {
      if (id == null) continue;
      const feature = results.find((f) => f.properties.region_id === id);
      if (!feature?.geometry) continue;
      ctx.strokeStyle = id === selectedId ? palette.select : palette.ink;
      ctx.lineWidth = id === selectedId ? 2.5 : 1.5;
      if (feature.properties.kind === "polygon") {
        ctx.beginPath();
        path(feature);
        ctx.stroke();
      } else {
        const point = projectVisible(feature.geometry.coordinates);
        if (!point) continue;
        ctx.beginPath();
        ctx.arc(point[0], point[1], 7.5, 0, TAU);
        ctx.stroke();
      }
    }

    // The rim, drawn last so nothing bleeds over the edge of the world.
    ctx.beginPath();
    path({ type: "Sphere" });
    ctx.strokeStyle = palette.landLine;
    ctx.lineWidth = 1;
    ctx.stroke();

    onCamera?.(scale / minScale);
  }

  // A point on the far side of the globe still projects to a coordinate inside
  // the disc - it is simply hidden by the planet. clipAngle handles this for
  // paths but not for a circle we draw ourselves.
  function projectVisible(coordinates) {
    const centre = [-rotation[0], -rotation[1]];
    if (window.d3.geoDistance(coordinates, centre) > Math.PI / 2) return null;
    return projection(coordinates);
  }

  function setScale(next) {
    scale = clamp(next, minScale, minScale * 60);
  }

  // Angular radius t fills a pixel radius of scale*sin(t); this is that solved
  // for scale, with the far side of the globe as the floor.
  function scaleForRadius(radians) {
    const pixels = Math.min(width, height) / 2;
    const angle = Math.min(Math.max(radians, MIN_FIT_RADIANS) * FIT_PADDING, Math.PI / 2);
    return pixels / Math.sin(angle);
  }

  function eachPosition(geometry, visit) {
    const walk = (coordinates, depth) => {
      if (depth === 0) visit(coordinates);
      else for (const child of coordinates) walk(child, depth - 1);
    };
    const depth = { Point: 0, MultiPoint: 1, Polygon: 2, MultiPolygon: 3 }[geometry.type];
    if (depth !== undefined) walk(geometry.coordinates, depth);
  }

  // Each feature reduced to a centroid and the angular distance from it to the
  // furthest point it contains.
  //
  // Not the spherical bounding box, which is what makes this worth spelling
  // out. Norway's bounding box includes Jan Mayen and Svalbard, so its corners
  // sit thousands of kilometres from any Norwegian soil; measured that way
  // Norway is not even visible from directly above Norway. Walking the real
  // vertices costs one pass over geometry we have already downloaded.
  function shapesOf(features) {
    return features
      .filter((feature) => feature.geometry)
      .map((feature) => {
        const centre = window.d3.geoCentroid(feature);
        let extent = 0;
        eachPosition(feature.geometry, (position) => {
          extent = Math.max(extent, window.d3.geoDistance(position, centre));
        });
        return { centre, extent };
      })
      .filter((shape) => Number.isFinite(shape.centre[0]));
  }

  // Upper bound by the triangle inequality: nothing in the shape is further
  // from `centre` than its own centroid is, plus its extent.
  function reachFrom(shape, centre) {
    return window.d3.geoDistance(shape.centre, centre) + shape.extent;
  }

  // Half the planet is always facing away, so "fit" cannot mean the same thing
  // it means on a flat map. Two rules, in order:
  //
  //   1. Show as many results as possible. A top ten of eight European
  //      countries, the United States and Australia cannot all be visible at
  //      once from any angle; the useful view is the one holding the eight,
  //      not the compromise that centres on empty ocean and shows three.
  //   2. Among the views that show the same number, take the tightest.
  //
  // When everything does fit - the common case - rule 1 ties for every
  // candidate and this is exactly a smallest-enclosing-cap fit. That is the
  // standard 1-centre approximation (test the overall centroid and each
  // feature's own centroid), within a factor of two of optimal and free at a
  // hundred features.
  function bestCentre(features) {
    const shapes = shapesOf(features);
    if (!shapes.length) return { centre: null, radius: 0 };
    const candidates = [
      window.d3.geoCentroid({ type: "FeatureCollection", features }),
      ...shapes.map((shape) => shape.centre),
    ];

    let best = null;
    for (const candidate of candidates) {
      if (!Number.isFinite(candidate[0]) || !Number.isFinite(candidate[1])) continue;
      // Judged on where a region sits, not how far its furthest island reaches:
      // a country is visible when it faces us, and its outlying territories
      // running over the horizon does not change that.
      const visible = shapes.filter(
        (shape) => window.d3.geoDistance(shape.centre, candidate) < VISIBLE_LIMIT
      );
      const measured = visible.length ? visible : shapes;
      const radius = Math.max(
        ...measured.map((shape) => reachFrom(shape, candidate))
      );
      if (
        !best ||
        visible.length > best.visible ||
        (visible.length === best.visible && radius < best.radius)
      ) {
        best = { centre: candidate, radius, visible: visible.length };
      }
    }
    return best;
  }

  function flyTo(features, { animate = true } = {}) {
    const { centre, radius } = bestCentre(features);
    if (!centre) return;
    const target = [-centre[0], -centre[1]];
    const targetScale = scaleForRadius(radius);
    if (!animate) {
      rotation = target;
      setScale(targetScale);
      draw();
      return;
    }
    animateTo(target, targetScale);
  }

  function animateTo(targetRotation, targetScale) {
    // A spinning globe is exactly the kind of motion this setting exists for.
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      rotation = targetRotation;
      setScale(targetScale);
      draw();
      return;
    }
    const from = { rotation: rotation.slice(), scale };
    const deltaLambda = shortestDelta(from.rotation[0], targetRotation[0]);
    const deltaPhi = targetRotation[1] - from.rotation[1];
    const start = performance.now();
    beginInteraction();
    animation = (now) => {
      // stopAnimation() clears this while a frame is already queued, and that
      // frame still runs — passing the now-null reference back to
      // requestAnimationFrame throws. Grabbing the globe mid-fly is the way in.
      if (!animation) return;
      const t = easeInOut(clamp((now - start) / FLY_MS, 0, 1));
      rotation = [from.rotation[0] + deltaLambda * t, from.rotation[1] + deltaPhi * t];
      // Interpolating the scale geometrically rather than linearly keeps the
      // apparent zoom rate constant; a linear ramp lurches at the wide end.
      scale = from.scale * Math.pow(targetScale / from.scale, t);
      render();
      if (t < 1) {
        requestAnimationFrame(animation);
      } else {
        animation = null;
        endInteraction();
      }
    };
    requestAnimationFrame(animation);
  }

  function stopAnimation() {
    animation = null;
  }

  function featureAt(x, y) {
    // Points win ties: a city marker sits on top of whatever polygon contains
    // it, so clicking the marker should not select the country underneath.
    for (const feature of results) {
      if (feature.properties.kind === "polygon" || !feature.geometry) continue;
      const point = projectVisible(feature.geometry.coordinates);
      if (point && Math.hypot(point[0] - x, point[1] - y) <= 9) return feature;
    }
    const geo = projection.invert([x, y]);
    if (!geo || !Number.isFinite(geo[0])) return null;
    for (const feature of results) {
      if (feature.properties.kind !== "polygon" || !feature.geometry) continue;
      if (window.d3.geoContains(feature, geo)) return feature;
    }
    return null;
  }

  function pointerPosition(event) {
    const rect = canvas.getBoundingClientRect();
    return [event.clientX - rect.left, event.clientY - rect.top];
  }

  let drag = null;

  canvas.addEventListener("pointerdown", (event) => {
    stopAnimation();
    beginInteraction();
    canvas.setPointerCapture(event.pointerId);
    drag = { start: pointerPosition(event), rotation: rotation.slice(), moved: false };
  });

  canvas.addEventListener("pointermove", (event) => {
    const [x, y] = pointerPosition(event);
    if (drag) {
      // Sensitivity falls with zoom so a pixel of drag always moves the surface
      // under the cursor about the same distance, however close in you are.
      const sensitivity = 75 / scale;
      const dx = x - drag.start[0];
      const dy = y - drag.start[1];
      if (Math.abs(dx) > 3 || Math.abs(dy) > 3) drag.moved = true;
      rotation = [
        drag.rotation[0] + dx * sensitivity,
        clamp(drag.rotation[1] - dy * sensitivity, -90, 90),
      ];
      draw();
      return;
    }
    const feature = featureAt(x, y);
    const id = feature ? feature.properties.region_id : null;
    if (id !== hoveredId) {
      hoveredId = id;
      canvas.style.cursor = id == null ? "grab" : "pointer";
      draw();
    }
    onHover?.(feature, { x, y });
  });

  canvas.addEventListener("pointerup", (event) => {
    const wasDrag = drag?.moved;
    drag = null;
    endInteraction();
    if (wasDrag) return;
    const feature = featureAt(...pointerPosition(event));
    onSelect?.(feature ? feature.properties.region_id : null);
  });

  canvas.addEventListener("pointerleave", () => {
    drag = null;
    endInteraction();
    if (hoveredId !== null) {
      hoveredId = null;
      draw();
    }
    onHover?.(null);
  });

  canvas.addEventListener(
    "wheel",
    (event) => {
      event.preventDefault();
      stopAnimation();
      // A wheel gesture is a burst of discrete events with no "end", so the
      // settle timer is what ends it.
      beginInteraction();
      endInteraction();
      const [x, y] = pointerPosition(event);
      const under = projection.invert([x, y]);
      setScale(scale * Math.exp(-event.deltaY * 0.0015));
      projection.scale(scale);
      // Keep whatever was under the cursor under the cursor. One correction
      // step is approximate but converges across a wheel gesture, and the
      // alternative - always zooming to the centre - fights the user.
      if (under && Number.isFinite(under[0])) {
        const after = projection.rotate([rotation[0], rotation[1]])(under);
        const sensitivity = 75 / scale;
        rotation = [
          rotation[0] + (x - after[0]) * sensitivity,
          clamp(rotation[1] - (y - after[1]) * sensitivity, -90, 90),
        ];
      }
      draw();
    },
    { passive: false }
  );

  new ResizeObserver(resize).observe(canvas);
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    palette = readPalette();
    draw();
  });

  return {
    // The draft is built once, from the first (coarsest) basemap supplied.
    // A later, finer basemap replaces what still frames draw but must not
    // become the draft: the whole point of the draft is to be the cheapest
    // usable version, and rebuilding it from denser geometry would quietly
    // give back the frame budget this exists to protect.
    setBasemap(collection) {
      basemap = collection;
      if (!basemapDraft) basemapDraft = buildDraft(collection);
      draw();
    },

    // Replacing the result set is also what clears the old one - there is no
    // separate teardown to forget to call.
    setResults(features, valuesById) {
      results = features.filter((f) => f.geometry);
      values = valuesById ?? new Map();
      const numbers = [...values.values()].filter(Number.isFinite);
      domain = numbers.length ? [Math.min(...numbers), Math.max(...numbers)] : null;
      selectedId = null;
      hoveredId = null;
      draw();
    },

    // Swapping in a higher-detail version of the same regions must not disturb
    // the camera or the selection - the user is mid-zoom when this arrives.
    refineResults(features) {
      const byId = new Map(features.map((f) => [f.properties.region_id, f]));
      results = results.map((f) => byId.get(f.properties.region_id) ?? f);
      draw();
    },

    select(regionId, { fly = true } = {}) {
      selectedId = regionId;
      const feature = results.find((f) => f.properties.region_id === regionId);
      if (feature && fly) flyTo([feature]);
      else draw();
    },

    fitResults(options) {
      if (!results.length) return;
      flyTo(results, options);
    },

    home() {
      stopAnimation();
      animateTo([-10, -15], minScale);
    },

    // "How far in are we" as a multiple of the whole-globe view, which is what
    // the detail-refetch threshold is expressed in.
    zoomFactor() {
      return scale / minScale;
    },

    hasResults() {
      return results.length > 0;
    },

    // The media query below covers the system preference changing. An explicit
    // theme choice moves the same tokens without it firing, so the page has to
    // say so — otherwise the map keeps yesterday's palette on a repainted page.
    refreshPalette() {
      palette = readPalette();
      draw();
    },
  };
}
