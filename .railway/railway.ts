// Railway Infrastructure as Code for the serving side: how Railway builds, runs and exposes
// the baked Datasette image. Preview a change with `railway config plan` and apply it with
// `railway config apply` from a checkout linked to the "malaria maps" project.
//
// Service variables (GEONAMES_USERNAME and friends) are deliberately unmanaged, so this file
// never reads or writes secrets. The restart policy (on failure, up to 10 retries) is a service
// setting kept in Railway itself.
import { defineRailway, github, project, service, volume } from "railway/iac";

// This repository manages only its own resources in the environment. Other
// repositories export their own partial name.
// See https://docs.railway.com/infrastructure-as-code#multi-repo-projects
export const partial = "malaria-maps";

export default defineRailway(() => {
  // Holds the runtime-written geocode cache (GEOCODE_CACHE_PATH in the Dockerfile), so it
  // survives deploys. Losing it is harmless: the cache refills from GeoNames on demand.
  const geocodeCache = volume("malaria-maps-volume", { region: "us-west2", sizeMB: 50000 });

  const malariaMaps = service("malaria-maps", {
    // The weekly collect workflow commits a rebaked data/malaria.db to main, and each push
    // there builds a new image.
    source: github("Fountain-Bio/malaria-maps", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    // /-/versions.json is a cheap 200 that doesn't touch the database.
    healthcheck: "/-/versions.json",
    healthcheckTimeout: 120,
    // One replica: the volume attaches to a single replica, and the geocode cache has a
    // single SQLite writer.
    replicas: { "us-west2": 1 },
    deploy: { drainingSeconds: 30, overlapSeconds: 0 },
    volumeMounts: { "/app/var": geocodeCache },
    domains: ["malariatracker.com"],
    cdn: { enabled: true },
  });

  return project("malaria maps", {
    variables: { managed: false },
    resources: [malariaMaps, geocodeCache],
  });
});
