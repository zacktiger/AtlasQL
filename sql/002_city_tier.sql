-- The city tier is point data: geom is NULL and centroid carries the location,
-- which the original schema already allows. What it lacks is an index on
-- centroid, and the city import needs one: assigning ~26,000 cities to their
-- state means a point-in-polygon join against 4,596 state polygons, which is a
-- sequential scan without it.

CREATE INDEX IF NOT EXISTS idx_regions_centroid ON regions USING GIST(centroid);
