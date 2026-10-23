"use strict";
// Lucide outline icons, ISC license: assets/Lucide-LICENSE.txt
const icons = {
  home: '<path d="M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8" />\n  <path d="M3 10a2 2 0 0 1 .709-1.528l7-6a2 2 0 0 1 2.582 0l7 6A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />',
  pay: '<path d="M7 7h10v10" />\n  <path d="M7 17 17 7" />',
  card: '<rect width="20" height="14" x="2" y="5" rx="2" />\n  <line x1="2" x2="22" y1="10" y2="10" />\n  <path d="M6 14h2" />',
  people:
    '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />\n  <path d="M16 3.128a4 4 0 0 1 0 7.744" />\n  <path d="M22 21v-2a4 4 0 0 0-3-3.87" />\n  <circle cx="9" cy="7" r="4" />',
  context:
    '<rect x="16" y="16" width="6" height="6" rx="1" />\n  <rect x="2" y="16" width="6" height="6" rx="1" />\n  <rect x="9" y="2" width="6" height="6" rx="1" />\n  <path d="M5 16v-3a1 1 0 0 1 1-1h12a1 1 0 0 1 1 1v3" />\n  <path d="M12 12V8" />',
  grid: '<rect width="7" height="7" x="3" y="3" rx="1" />\n  <rect width="7" height="7" x="14" y="3" rx="1" />\n  <rect width="7" height="7" x="14" y="14" rx="1" />\n  <rect width="7" height="7" x="3" y="14" rx="1" />',
  settings:
    '<path d="M14 17H5" />\n  <path d="M19 7h-9" />\n  <circle cx="17" cy="17" r="3" />\n  <circle cx="7" cy="7" r="3" />',
  bell: '<path d="M10.268 21a2 2 0 0 0 3.464 0" />\n  <path d="M3.262 15.326A1 1 0 0 0 4 17h16a1 1 0 0 0 .74-1.673C19.41 13.956 18 12.499 18 8A6 6 0 0 0 6 8c0 4.499-1.411 5.956-2.738 7.326" />',
  down: '<path d="m6 9 6 6 6-6" />',
  right: '<path d="m9 18 6-6-6-6" />',
  left: '<path d="m15 18-6-6 6-6" />',
  close: '<path d="M18 6 6 18" />\n  <path d="m6 6 12 12" />',
  minus: '<path d="M5 12h14" />',
  plus: '<path d="M5 12h14" />\n  <path d="M12 5v14" />',
  book: '<path d="M17 3a2 2 0 0 1 2 2v15a1 1 0 0 1-1.496.868l-4.512-2.578a2 2 0 0 0-1.984 0l-4.512 2.578A1 1 0 0 1 5 20V5a2 2 0 0 1 2-2z" />',
  more: '<circle cx="12" cy="12" r="1" />\n  <circle cx="19" cy="12" r="1" />\n  <circle cx="5" cy="12" r="1" />',
  chat: '<path d="M2.992 16.342a2 2 0 0 1 .094 1.167l-1.065 3.29a1 1 0 0 0 1.236 1.168l3.413-.998a2 2 0 0 1 1.099.092 10 10 0 1 0-4.777-4.719" />',
  send: '<path d="M14.536 21.686a.5.5 0 0 0 .937-.024l6.5-19a.496.496 0 0 0-.635-.635l-19 6.5a.5.5 0 0 0-.024.937l7.93 3.18a2 2 0 0 1 1.112 1.11z" />\n  <path d="m21.854 2.147-10.94 10.939" />',
  mic: '<path d="M12 19v3" />\n  <path d="M19 10v2a7 7 0 0 1-14 0v-2" />\n  <rect x="9" y="2" width="6" height="13" rx="3" />',
  trend: '<path d="M16 7h6v6" />\n  <path d="m22 7-8.5 8.5-5-5L2 17" />',
  arrows:
    '<path d="m21 16-4 4-4-4" />\n  <path d="M17 20V4" />\n  <path d="m3 8 4-4 4 4" />\n  <path d="M7 4v16" />',
  check: '<path d="M20 6 9 17l-5-5" />',
  building:
    '<path d="M12 10h.01" />\n  <path d="M12 14h.01" />\n  <path d="M12 6h.01" />\n  <path d="M16 10h.01" />\n  <path d="M16 14h.01" />\n  <path d="M16 6h.01" />\n  <path d="M8 10h.01" />\n  <path d="M8 14h.01" />\n  <path d="M8 6h.01" />\n  <path d="M9 22v-3a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v3" />\n  <rect x="4" y="2" width="16" height="20" rx="2" />',
  link: '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />\n  <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />',
  clock: '<circle cx="12" cy="12" r="10" />\n  <path d="M12 6v6l4 2" />',
  info: '<circle cx="12" cy="12" r="10" />\n  <path d="M12 16v-4" />\n  <path d="M12 8h.01" />',
  refresh:
    '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8" />\n  <path d="M21 3v5h-5" />\n  <path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16" />\n  <path d="M8 16H3v5" />',
  menu: '<path d="M4 5h16" />\n  <path d="M4 12h16" />\n  <path d="M4 19h16" />',
  volume:
    '<path d="M11 4.702a.705.705 0 0 0-1.203-.498L6.413 7.587A1.4 1.4 0 0 1 5.416 8H3a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2.416a1.4 1.4 0 0 1 .997.413l3.383 3.384A.705.705 0 0 0 11 19.298z" />\n  <path d="M16 9a5 5 0 0 1 0 6" />\n  <path d="M19.364 18.364a9 9 0 0 0 0-12.728" />',
  stop: '<rect width="18" height="18" x="3" y="3" rx="2" />',
  trash:
    '<path d="M10.733 5.076a10.744 10.744 0 0 1 11.205 6.575 1 1 0 0 1 0 .696 10.747 10.747 0 0 1-1.444 2.49" />\n  <path d="M14.084 14.158a3 3 0 0 1-4.242-4.242" />\n  <path d="M17.479 17.499a10.75 10.75 0 0 1-15.417-5.151 1 1 0 0 1 0-.696 10.75 10.75 0 0 1 4.446-5.143" />\n  <path d="m2 2 20 20" />',
  eyeoff:
    '<path d="M10.733 5.076a10.744 10.744 0 0 1 11.205 6.575 1 1 0 0 1 0 .696 10.747 10.747 0 0 1-1.444 2.49" />\n  <path d="M14.084 14.158a3 3 0 0 1-4.242-4.242" />\n  <path d="M17.479 17.499a10.75 10.75 0 0 1-15.417-5.151 1 1 0 0 1 0-.696 10.75 10.75 0 0 1 4.446-5.143" />\n  <path d="m2 2 20 20" />',
  pencil:
    '<path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z" />\n  <path d="m15 5 4 4" />',
  package:
    '<path d="M11 21.73a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73z" />\n  <path d="M12 22V12" />\n  <polyline points="3.29 7 12 12 20.71 7" />\n  <path d="m7.5 4.27 9 5.15" />',
  file: '<path d="M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z" />\n  <path d="M14 2v5a1 1 0 0 0 1 1h5" />\n  <path d="M10 9H8" />\n  <path d="M16 13H8" />\n  <path d="M16 17H8" />',
};

icons["search"] =
  '\n  <circle cx="11" cy="11" r="8" />\n  <path d="m21 21-4.3-4.3" />\n';

icons["calendar"] =
  '\n  <path d="M8 2v4" />\n  <path d="M16 2v4" />\n  <rect width="18" height="18" x="3" y="4" rx="2" />\n  <path d="M3 10h18" />\n  <path d="M8 14h.01" />\n  <path d="M12 14h.01" />\n  <path d="M16 14h.01" />\n  <path d="M8 18h.01" />\n  <path d="M12 18h.01" />\n  <path d="M16 18h.01" />\n';

// Lucide Maximize2 / Minimize2, same ISC license as the icon set.
icons.maximize =
  '<path d="M15 3h6v6"/><path d="m21 3-7 7"/><path d="M3 15v6h6"/><path d="m3 21 7-7"/>';
icons.minimize =
  '<path d="m14 10 7-7"/><path d="M20 10h-6V4"/><path d="m3 21 7-7"/><path d="M4 14h6v6"/>';

export { icons };
