# AtlasQL

## Overview

AtlasQL is a geographic intelligence platform that allows users to search and analyze the world using structured conditions instead of manually browsing maps or datasets.

Rather than limiting users to predefined statistics, AtlasQL lets them ask flexible questions across different geographic levels, including continents, countries, states, counties, cities, and eventually natural features such as rivers, mountains, and lakes.

### Example Queries

- Countries with GDP per capita above **$40,000** and average elevation above **500 meters**
- Cities above **2,000 meters** with populations greater than **500,000**
- States containing more than **five major rivers**
- Rivers longer than **4,000 km**
- Countries similar to **Chile** but with warmer climates

The goal is to make geographic data searchable in the same way databases make structured information searchable.

---

# Vision

Most geographic information exists in isolated datasets.

- Population comes from one source.
- Elevation from another.
- Administrative boundaries from another.
- Economic indicators from another.

TerraQuery brings these datasets together into a unified geographic knowledge base where every region and, eventually, every physical feature can be queried through a common interface.

Instead of searching for information dataset by dataset, users search the world itself.

---

# Core Principles

## Unified Geographic Database

Integrate diverse geographic, demographic, economic, environmental, and spatial datasets into a common structure.

---

## Flexible Query Engine

Allow users to combine multiple conditions without predefined templates.

### Example Filters

- GDP > X
- Population < Y
- Elevation ≥ Z
- River Count ≥ N

Queries should work consistently across multiple administrative levels whenever data is available.

---

## Intelligent Level Selection

Users shouldn't always need to specify whether they're searching countries, states, or cities.

The engine automatically determines the most appropriate geographic level based on metric availability while still allowing manual selection.

---

## Natural Language + Structured Search

Natural language serves as a convenience layer, not the core engine.

Users may either:

- Build queries through filters
- Write natural language
- Edit AI-generated filters before execution

Both methods ultimately produce the same structured query.

---

## Extensible Architecture

New datasets and metrics should be addable without redesigning the system.

The same engine should eventually support:

- Countries
- States
- Counties
- Cities
- Rivers
- Mountains
- Lakes
- Forests
- Deserts
- Protected Areas

using a common querying framework.

---

# Long-Term Goal

Move beyond simple filtering toward geographic reasoning.

### Future Capabilities

- "Rich mountainous countries"
- "Countries similar to Japan"
- "Cities like Zurich but warmer"
- "Regions with geography similar to Nepal"

These queries rely on ranking, similarity, and multidimensional comparisons rather than fixed thresholds.

---

# Development Philosophy

The platform is built incrementally.

1. Build a reliable structured query engine.
2. Expand geographic coverage.
3. Add additional geographic entities.
4. Introduce natural language.
5. Develop ranking and similarity search.

Each stage builds upon the previous one, ensuring correctness before adding more advanced capabilities.