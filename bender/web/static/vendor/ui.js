// Vendored without a build step: preact 10.29.8, preact/hooks 10.29.8 (MIT), htm 3.1.1 (Apache-2.0); see THIRD_PARTY_NOTICES.md.
export * from "./preact.module.js";
export * from "./hooks.module.js";
import { h } from "./preact.module.js";
import htm from "./htm.module.js";
export const html = htm.bind(h);
