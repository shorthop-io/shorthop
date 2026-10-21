/** The portal's icon set — one inline SVG sprite-less component.
 *
 *  ⚠️ **Copy-paste-per-portal** (like `units.tsx`, and `db.py`/`metrics.py` on the backends):
 *  this copy came from the marina portal, which had itself started as myboat's. It is
 *  therefore the UNION of both sets: fleet shows marina-shaped surfaces (crew, money,
 *  records) and myboat-shaped ones (a boat's tanks, power flow, engine), so trimming it to
 *  "what fleet renders today" would only mean redrawing a glyph the next port needs. Keep
 *  shared geometry identical across the copies — an anchor must not be a different anchor
 *  here than in myboat — and add new shapes to the marked sections rather than reordering
 *  what is already there.
 *
 *  Why not emoji (which is what this replaced): emoji render with a different typeface,
 *  weight and colour on every OS, ignore `currentColor` so they can never pick up the
 *  active-nav or muted-text colour, and sit on their own baseline — which is why the
 *  sidebar and the metric tiles looked like a prototype. These are 24×24, stroke-only,
 *  inherit `currentColor` and scale with `size`.
 *
 *  Geometry follows the Feather icon set's conventions (24px grid, 1.75 stroke, round
 *  caps/joins) so a name added later looks like it belongs without redrawing anything.
 */

export type IconName =
  // schematics (from yacht-portal)
  | "flame" | "layers"
  // shell / navigation
  | "compass" | "route" | "wrench" | "sailboat" | "plug" | "plus" | "map" | "anchor"
  | "user" | "card" | "bell" | "building" | "signout" | "menu" | "file" | "qr"
  // boat sections
  | "pin" | "book" | "alert" | "antenna" | "share" | "chart"
  // metrics
  | "speed" | "depth" | "wind" | "battery" | "gear" | "thermometer" | "fuel"
  | "droplet" | "waves"
  // document kinds
  | "manual" | "wiring" | "idcard" | "shield" | "search" | "receipt" | "photo"
  // weather / logbook kinds
  | "sun" | "moon" | "cloud" | "note"
  // org + boat types
  | "motorboat" | "star"
  // berth / yard / sublet
  | "clock" | "flag" | "money" | "crane" | "truck" | "arrowUp" | "arrowDown"
  // berth amenities (free-text `utilities` tokens)
  | "bolt" | "wifi" | "pumpout"
  // transmission
  | "satellite" | "sparkle" | "phone" | "move"
  // affordances
  | "eye" | "download" | "upload" | "link" | "printer" | "check" | "question"
  | "x" | "edit" | "chevron" | "chevronRight" | "chevronLeft" | "dot"
  // --- myboat additions (the per-boat subsystem panels) ---
  | "dc"    // power flow: the DC bus
  | "oil"   // tanks: fuel/droplet/waves/pumpout cover the rest
  // --- marina additions ---
  // owner comms
  | "megaphone" | "chat" | "mail"
  // access control + notification state
  | "key" | "door" | "ticket" | "ban" | "bellOff"
  // records / inventory
  | "calendar" | "clipboard" | "scroll" | "box" | "tag"
  // quayside + yard work
  | "hammer" | "ruler" | "trash" | "refresh" | "flask"
  // money + analytics
  | "trendUp" | "swap" | "users"
  // misc signals
  | "signature" | "home" | "globe" | "radio" | "bulb" | "thumbUp" | "thumbDown"
  | "display"
  // weather conditions (WMO code buckets — see Weather.tsx)
  | "cloudSun" | "cloudDrizzle" | "rain" | "snow" | "storm" | "fog";

/** Path data only — the <svg> wrapper (viewBox, stroke, caps) is shared below, so an
 *  icon can never disagree with the rest of the set about stroke width or grid. */
const PATHS: Record<IconName, JSX.Element> = {
  // --- shell / navigation ---
  compass: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M15.8 8.2l-2.2 5.4-5.4 2.2 2.2-5.4z" />
    </>
  ),
  route: (
    <>
      {/* Three waypoints on a leg. Filled dots, not rings, and NO dash: at 17px — the
          size the tab strip actually renders — a dashed path turns to mush and rings
          close up into blobs. Verified against the alternatives at 15/17/20/40. */}
      <circle cx="5" cy="19" r="2.2" fill="currentColor" stroke="none" />
      <circle cx="12" cy="12" r="2.2" fill="currentColor" stroke="none" />
      <circle cx="19" cy="5" r="2.2" fill="currentColor" stroke="none" />
      <path d="M6.6 17.4l3.8-3.8M13.6 10.4l3.8-3.8" />
    </>
  ),
  wrench: (
    <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94z" />
  ),
  sailboat: (
    <>
      <path d="M11 2.5v12" />
      <path d="M13 14.5V4.5l6.5 10z" />
      <path d="M3.5 17.5h17l-2.3 3.5H5.8z" />
    </>
  ),
  plug: (
    <>
      <path d="M9 2.5v5M15 2.5v5" />
      <path d="M6 7.5h12v3.5a6 6 0 0 1-12 0z" />
      <path d="M12 17v4.5" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  map: (
    <>
      <path d="M1.5 6.5l7-3 7 3 7-3v14l-7 3-7-3-7 3z" />
      <path d="M8.5 3.5v14M15.5 6.5v14" />
    </>
  ),
  anchor: (
    <>
      <circle cx="12" cy="5" r="2.6" />
      <path d="M12 7.6V21" />
      <path d="M5 12.5H2.2a9.8 9.8 0 0 0 19.6 0H19" />
    </>
  ),
  user: (
    <>
      <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
      <circle cx="12" cy="7" r="4" />
    </>
  ),
  card: (
    <>
      <rect x="1.5" y="4.5" width="21" height="15" rx="2.5" />
      <path d="M1.5 10h21" />
    </>
  ),
  bell: (
    <>
      <path d="M18 8.5a6 6 0 1 0-12 0c0 6.5-2.5 8.5-2.5 8.5h17S18 15 18 8.5z" />
      <path d="M13.7 20.5a2 2 0 0 1-3.4 0" />
    </>
  ),
  building: (
    <>
      <path d="M4.5 21V4.5a1.5 1.5 0 0 1 1.5-1.5h8a1.5 1.5 0 0 1 1.5 1.5V21" />
      <path d="M15.5 10h3a1.5 1.5 0 0 1 1.5 1.5V21" />
      <path d="M2.5 21h19" />
      <path d="M8 7h4M8 11h4M8 15h4" />
    </>
  ),
  signout: (
    <>
      <path d="M9.5 21H5.5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
      <path d="M16.5 16.5l5-4.5-5-4.5" />
      <path d="M21.5 12h-12" />
    </>
  ),
  menu: <path d="M3.5 6.5h17M3.5 12h17M3.5 17.5h17" />,
  file: (
    <>
      <path d="M14 2.5H6.5a2 2 0 0 0-2 2v15a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2V8z" />
      <path d="M14 2.5V8h5.5" />
      <path d="M8.5 13h7M8.5 17h4.5" />
    </>
  ),

  // --- boat sections ---
  pin: (
    <>
      <path d="M20.5 10.5c0 6.5-8.5 12-8.5 12s-8.5-5.5-8.5-12a8.5 8.5 0 0 1 17 0z" />
      <circle cx="12" cy="10.5" r="2.8" />
    </>
  ),
  book: (
    <>
      <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" />
      <path d="M6.5 2.5H20v19H6.5A2.5 2.5 0 0 1 4 19V5a2.5 2.5 0 0 1 2.5-2.5z" />
    </>
  ),
  alert: (
    <>
      <path d="M10.3 3.9L2 18a2 2 0 0 0 1.7 3h16.6a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" />
      <path d="M12 9v4M12 17h.01" />
    </>
  ),
  antenna: (
    <>
      <circle cx="12" cy="12" r="2" />
      <path d="M16.2 7.8a6 6 0 0 1 0 8.4M7.8 16.2a6 6 0 0 1 0-8.4" />
      <path d="M19.1 4.9a10 10 0 0 1 0 14.2M4.9 19.1a10 10 0 0 1 0-14.2" />
    </>
  ),
  share: (
    <>
      <circle cx="18" cy="5" r="2.6" />
      <circle cx="6" cy="12" r="2.6" />
      <circle cx="18" cy="19" r="2.6" />
      <path d="M8.3 10.7l7.4-4.4M8.3 13.3l7.4 4.4" />
    </>
  ),
  chart: (
    <>
      <path d="M3 3v18h18" />
      <path d="M7 15l4-5 3.5 3L20 6" />
    </>
  ),

  // --- metrics ---
  speed: (
    <>
      <path d="M4.2 18a9 9 0 1 1 15.6 0" />
      <path d="M12 12.5l4.2-3.4" />
      <circle cx="12" cy="13" r="1.3" />
    </>
  ),
  depth: (
    <>
      <path d="M12 3v11" />
      <path d="M8 10.5l4 4 4-4" />
      <path d="M3.5 19.5c2-1.8 4-1.8 6 0s4 1.8 6 0 4-1.8 5-0.6" />
    </>
  ),
  wind: (
    <path d="M9.6 4.6A2 2 0 1 1 11 8H2.5m10.1 11.4A2 2 0 1 0 14 16H2.5m15.2-8.3A2.5 2.5 0 1 1 19.5 12H2.5" />
  ),
  battery: (
    <>
      <rect x="1.5" y="7" width="17" height="10" rx="2" />
      <path d="M22 10.5v3" />
      <path d="M5 10.5v3M8.5 10.5v3M12 10.5v3" />
    </>
  ),
  gear: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6h.09A1.65 1.65 0 0 0 10.6 3.09V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </>
  ),
  thermometer: (
    <path d="M14 14.8V3.5a2.5 2.5 0 0 0-5 0v11.3a4.5 4.5 0 1 0 5 0z" />
  ),
  fuel: (
    <>
      <path d="M3.5 21V4.5a2 2 0 0 1 2-2h5a2 2 0 0 1 2 2V21" />
      <path d="M2.5 21h11M3.5 10h9" />
      <path d="M15.5 8H17a2 2 0 0 1 2 2v7a1.6 1.6 0 0 0 3.2 0v-5l-2-3.5" />
    </>
  ),
  droplet: <path d="M12 2.7l5.7 5.7a8 8 0 1 1-11.4 0z" />,
  waves: (
    <path d="M2.5 8.5c2-2 4-2 6 0s4 2 6 0 4-2 6 0M2.5 15c2-2 4-2 6 0s4 2 6 0 4-2 6 0" />
  ),

  // --- document kinds ---
  manual: (
    <>
      <path d="M12 6.5C10.5 5 8.5 4.5 4 4.5v13c4.5 0 6.5.5 8 2 1.5-1.5 3.5-2 8-2v-13c-4.5 0-6.5.5-8 2z" />
      <path d="M12 6.5v13" />
    </>
  ),
  wiring: (
    <>
      <path d="M4 6h5a3 3 0 0 1 3 3v6a3 3 0 0 0 3 3h5" />
      <circle cx="4" cy="6" r="1.8" />
      <circle cx="20" cy="18" r="1.8" />
    </>
  ),
  idcard: (
    <>
      <rect x="2.5" y="5" width="19" height="14" rx="2.5" />
      <circle cx="8.5" cy="11" r="2" />
      <path d="M5.5 16c.8-1.4 1.8-2 3-2s2.2.6 3 2M14.5 10h4M14.5 13.5h4" />
    </>
  ),
  shield: (
    <>
      <path d="M12 2.5l8 3v6c0 5-3.4 8.6-8 10-4.6-1.4-8-5-8-10v-6z" />
      <path d="M9 12l2.2 2.2L15.5 10" />
    </>
  ),
  search: (
    <>
      <circle cx="10.5" cy="10.5" r="6.5" />
      <path d="M15.5 15.5l5 5" />
    </>
  ),
  receipt: (
    <>
      <path d="M5 2.5h14v19l-3.5-2-3.5 2-3.5-2L5 21.5z" />
      <path d="M8.5 8h7M8.5 12h7M8.5 16h4" />
    </>
  ),
  photo: (
    <>
      <rect x="2.5" y="4.5" width="19" height="15" rx="2.5" />
      <circle cx="8.5" cy="10" r="1.8" />
      <path d="M2.5 16.5l5-4 4.5 3.5 3.5-3 6 5" />
    </>
  ),

  // --- affordances ---
  eye: (
    <>
      <path d="M1.5 12S5.5 5 12 5s10.5 7 10.5 7-4 7-10.5 7S1.5 12 1.5 12z" />
      <circle cx="12" cy="12" r="3" />
    </>
  ),
  download: (
    <>
      <path d="M21 15.5V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-3.5" />
      <path d="M7.5 11l4.5 4.5 4.5-4.5" />
      <path d="M12 15.5V3" />
    </>
  ),
  x: <path d="M6 6l12 12M18 6L6 18" />,
  qr: (
    <>
      <rect x="3.5" y="3.5" width="6.5" height="6.5" rx="1" />
      <rect x="14" y="3.5" width="6.5" height="6.5" rx="1" />
      <rect x="3.5" y="14" width="6.5" height="6.5" rx="1" />
      <path d="M14 14h2.5M20.5 14v2.5M14 17.5v3h3M20.5 19.5v1h-1M17.5 17.3h.01" />
    </>
  ),
  edit: (
    <>
      <path d="M17 3.5a2.8 2.8 0 0 1 4 4L7.5 21 2.5 22.5 4 17.5z" />
      <path d="M15 5.5l3.5 3.5" />
    </>
  ),
  flame: (
    <path d="M12 21.5c3.6 0 5.8-2.4 5.8-5.4 0-4.4-4.3-6-3.4-10.6C11.3 6.6 9.6 9.1 9.6 11.3c0 1.2.5 2 .5 2.8 0 .9-.7 1.5-1.5 1.5-1 0-1.7-.9-1.7-2.2-.4.8-.7 1.9-.7 3 0 3 2.2 5.1 5.8 5.1z" />
  ),
  layers: (
    <>
      <path d="M12 2.8 3 7.4l9 4.6 9-4.6z" />
      <path d="M3 12.2l9 4.6 9-4.6M3 16.8l9 4.6 9-4.6" />
    </>
  ),
  chevron: <path d="M6 9.5l6 6 6-6" />,

  // --- weather / logbook kinds ---
  sun: (
    <>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2.5v2.5M12 19v2.5M2.5 12H5M19 12h2.5M5.2 5.2l1.8 1.8M17 17l1.8 1.8M18.8 5.2L17 7M7 17l-1.8 1.8" />
    </>
  ),
  chevronRight: (
    // A row that NAVIGATES, not one that expands. The down-chevron on the Today worklist read
    // as "click to reveal more here", which is the opposite of what those rows do.
    <path d="M9.5 5.5l7 6.5-7 6.5" />
  ),
  // The booking page's gallery and calendar step BACK — a mirrored chevronRight, drawn rather
  // than CSS-rotated so the icon stays one glyph with one meaning.
  chevronLeft: <path d="M14.5 5.5l-7 6.5 7 6.5" />,
  moon: <path d="M20.5 15.6A8.6 8.6 0 0 1 8.9 4 8.6 8.6 0 1 0 20.5 15.6z" />,
  cloud: <path d="M6.8 18.5h10.7a3.6 3.6 0 0 0 .2-7.2 5.7 5.7 0 0 0-10.9-1.3 4.3 4.3 0 0 0 0 8.5z" />,
  note: (
    <>
      <rect x="4" y="3.5" width="16" height="17" rx="2" />
      <path d="M8 9h8M8 13h8M8 17h4.5" />
    </>
  ),

  // --- org + boat types ---
  // A motor boat, kept deliberately mast-less so it can never be mistaken for `sailboat`
  // at 15px — the two sit next to each other in the org list (fleet vs a berth holder).
  motorboat: (
    <>
      <path d="M3 17.5h18l-2.3 3.5H5.3z" />
      <path d="M7.5 17.5v-4.5h5.5l3.5 4.5" />
      <path d="M10 13v-3.5" />
    </>
  ),
  star: <path d="M12 3.2l2.85 5.8 6.4.93-4.62 4.5 1.09 6.35L12 17.75 6.28 20.78l1.09-6.35-4.62-4.5 6.4-.93z" />,

  // --- berth / yard / sublet ---
  clock: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7.2V12l3.6 2.2" />
    </>
  ),
  flag: (
    <>
      <path d="M5.5 21V3.5" />
      <path d="M5.5 4.5h10.8l-1.4 3.2 1.4 3.2H5.5z" />
    </>
  ),
  money: (
    <>
      <rect x="2.5" y="6" width="19" height="12" rx="2.5" />
      <circle cx="12" cy="12" r="2.6" />
      <path d="M6 12h.01M18 12h.01" />
    </>
  ),
  crane: (
    <>
      {/* Mast, jib and a hanging block. The first draft added a diagonal brace, which read
          as a flag on a pole once the strokes crowded at 14-16px — the size the yard
          section renders at. A gantry frame is the more accurate shape for a travel lift
          but is denser still at that size. */}
      <path d="M7 21V4h12" />
      <path d="M4 21h7" />
      <path d="M16.5 4v5.5" />
      <path d="M14.9 9.5h3.2v3h-3.2z" />
    </>
  ),
  truck: (
    <>
      <rect x="1.5" y="7" width="12" height="8.5" rx="1.5" />
      <path d="M13.5 10.5h3.8l3.2 3.5v1.5h-7z" />
      <circle cx="6" cy="18" r="1.9" />
      <circle cx="17" cy="18" r="1.9" />
    </>
  ),
  arrowUp: <path d="M12 20V4.5M6 10.5l6-6 6 6" />,
  arrowDown: <path d="M12 4v15.5M6 13.5l6 6 6-6" />,

  // --- berth amenities ---
  bolt: <path d="M13.6 2.5L4 14h6l-1.6 7.5L20 10h-6.2z" />,
  wifi: (
    <>
      <path d="M8.6 15.4a5 5 0 0 1 6.8 0" />
      <path d="M5.6 12.3a9.2 9.2 0 0 1 12.8 0" />
      <path d="M2.6 9.2a13.4 13.4 0 0 1 18.8 0" />
      <circle cx="12" cy="18.4" r="1.2" fill="currentColor" stroke="none" />
    </>
  ),
  pumpout: (
    <>
      <path d="M12 3v7.5" />
      <path d="M8.5 7l3.5 3.5L15.5 7" />
      <path d="M4.5 13.5h15v5a2 2 0 0 1-2 2h-11a2 2 0 0 1-2-2z" />
    </>
  ),

  // --- transmission ---
  // Signal radiating from a fixed point: the GPS/satellite badge (an auto-derived trip,
  // a shared position). `antenna` is the two-sided version and means "device".
  satellite: (
    <>
      <circle cx="5.5" cy="18.5" r="1.6" fill="currentColor" stroke="none" />
      <path d="M5.5 13.6a4.9 4.9 0 0 1 4.9 4.9" />
      <path d="M5.5 8.7a9.8 9.8 0 0 1 9.8 9.8" />
      <path d="M5.5 3.8a14.7 14.7 0 0 1 14.7 14.7" />
    </>
  ),
  sparkle: (
    <>
      <path d="M10 3l1.7 4.3L16 9l-4.3 1.7L10 15l-1.7-4.3L4 9l4.3-1.7z" />
      <path d="M17.5 14.5l.85 2.15L20.5 17.5l-2.15.85L17.5 20.5l-.85-2.15L14.5 17.5l2.15-.85z" />
    </>
  ),

  // --- more affordances ---
  upload: (
    <>
      <path d="M21 15.5V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-3.5" />
      <path d="M7.5 8L12 3.5 16.5 8" />
      <path d="M12 3.5V16" />
    </>
  ),
  link: (
    <>
      <path d="M10.2 13.8a4.2 4.2 0 0 0 5.9 0l2.7-2.7a4.2 4.2 0 0 0-5.9-5.9l-1.3 1.3" />
      <path d="M13.8 10.2a4.2 4.2 0 0 0-5.9 0l-2.7 2.7a4.2 4.2 0 0 0 5.9 5.9l1.3-1.3" />
    </>
  ),
  printer: (
    <>
      <path d="M6.5 9.5V3.5h11v6" />
      <path d="M6.5 17.5H5a2 2 0 0 1-2-2v-4a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2h-1.5" />
      <rect x="6.5" y="14.5" width="11" height="6" rx="1" />
    </>
  ),
  check: <path d="M4.5 12.5l5 5 10-11" />,
  question: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M9.4 9.3a2.7 2.7 0 0 1 5.3.9c0 1.8-2.7 2.2-2.7 4" />
      <path d="M12 17.4h.01" />
    </>
  ),
  dot: <circle cx="12" cy="12" r="2.5" fill="currentColor" stroke="none" />,
  move: (
    <>
      <path d="M12 3v18M3 12h18" />
      <path d="M9 6l3-3 3 3M9 18l3 3 3-3M6 9l-3 3 3 3M18 9l3 3-3 3" />
    </>
  ),
  phone: (
    <>
      <rect x="6" y="2.5" width="12" height="19" rx="2.5" />
      <path d="M10.5 18.5h3" />
    </>
  ),

  // ======================= myboat additions (boat subsystems) =======================
  // A DC bus: one solid rail over a dashed one, the IEC symbol for direct current. Reads as
  // distinct from `bolt` (power in general) at the 15px it spends its life at.
  dc: (
    <>
      <path d="M4 9.5h16" />
      <path d="M4 14.5h3M10.5 14.5h3M17 14.5h3" />
    </>
  ),
  // An oil can — the one tank type with no existing glyph, and it must not be another
  // droplet, because the Tanks panel shows oil and fresh water side by side.
  oil: (
    <>
      <path d="M3 17.5v-6h7l3 2h5a3 3 0 0 1 3 3v1a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
      <path d="M13 13.5l6-4.5M6.5 11.5v-2h4v2" />
    </>
  ),

  // ============================ marina additions ============================
  // --- owner comms ---
  megaphone: (
    <>
      <path d="M4 10.2v3.6h2.6L13 18V6l-6.4 4.2z" />
      <path d="M16.6 8.6a4.6 4.6 0 0 1 0 6.8" />
    </>
  ),
  chat: (
    <>
      {/* Squared bubble, not a circle: at 15px a round bubble with a tail reads as a blob,
          and the marina portal renders this in table rows and unread badges. */}
      <path d="M4 5.5h16a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1H9l-4 3.5V15.5H4a1 1 0 0 1-1-1v-8a1 1 0 0 1 1-1z" />
      <path d="M7.5 9.5h9M7.5 12h5.5" />
    </>
  ),
  mail: (
    <>
      <rect x="3" y="5.5" width="18" height="13" rx="1.5" />
      <path d="M3.6 6.6L12 12.8l8.4-6.2" />
    </>
  ),
  // --- access control + notification state ---
  key: (
    <>
      <circle cx="8" cy="8" r="3.6" />
      <path d="M10.6 10.6L20 20" />
      <path d="M16.4 16.4l2.1-2.1M13.9 13.9l2.1-2.1" />
    </>
  ),
  door: (
    <>
      <path d="M5.5 21V4a1 1 0 0 1 1-1h11a1 1 0 0 1 1 1v17" />
      <path d="M3.5 21h17" />
      <circle cx="15" cy="12.5" r="1.1" fill="currentColor" stroke="none" />
    </>
  ),
  ticket: (
    <>
      <path d="M3 8.5V6a1 1 0 0 1 1-1h16a1 1 0 0 1 1 1v2.5a2.5 2.5 0 0 0 0 7V18a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-2.5a2.5 2.5 0 0 0 0-7z" />
      <path d="M9.5 9v6" />
    </>
  ),
  ban: (
    <>
      <circle cx="12" cy="12" r="8.8" />
      <path d="M5.8 5.8l12.4 12.4" />
    </>
  ),
  bellOff: (
    <>
      {/* The muted twin of `bell`, so a notify-on/off pair reads as one control. */}
      <path d="M8.2 6.1A5.6 5.6 0 0 1 17.6 10c0 5.2 2 6.5 2 6.5H7.5" />
      <path d="M6.1 10.6C6 13.9 4.4 16.5 4.4 16.5h3" />
      <path d="M10.3 20a2 2 0 0 0 3.4 0" />
      <path d="M3.5 3.5l17 17" />
    </>
  ),
  // --- records / inventory ---
  calendar: (
    <>
      <rect x="3.5" y="5.5" width="17" height="15" rx="1.8" />
      <path d="M3.5 10.5h17M8.5 3.2v4M15.5 3.2v4" />
    </>
  ),
  clipboard: (
    <>
      <path d="M9 4.5H7a1.5 1.5 0 0 0-1.5 1.5v13A1.5 1.5 0 0 0 7 20.5h10a1.5 1.5 0 0 0 1.5-1.5V6A1.5 1.5 0 0 0 17 4.5h-2" />
      <rect x="9" y="2.8" width="6" height="3.4" rx="1" />
      <path d="M8.8 11h6.4M8.8 14.5h4.4" />
    </>
  ),
  scroll: (
    <>
      {/* A sealed agreement. NOT a rolled scroll: the curl read as a border at 15px, and
          not `file` either — the seal is what says "signed" rather than "document". */}
      <rect x="4.8" y="3" width="14.4" height="18" rx="1.6" />
      <path d="M8 7.6h8M8 11.2h8M8 14.8h3.6" />
      <circle cx="16" cy="17.2" r="2.1" />
    </>
  ),
  box: (
    <>
      <path d="M3.5 8.2L12 4l8.5 4.2v7.6L12 20l-8.5-4.2z" />
      <path d="M3.5 8.2L12 12.4l8.5-4.2M12 12.4V20" />
    </>
  ),
  tag: (
    <>
      <path d="M11.3 3.5H19a1.5 1.5 0 0 1 1.5 1.5v7.7a1 1 0 0 1-.3.7l-7.6 7.6a1 1 0 0 1-1.4 0l-7.4-7.4a1 1 0 0 1 0-1.4l7.8-8.4a1 1 0 0 1 .7-.3z" />
      <circle cx="16.2" cy="7.8" r="1.4" />
    </>
  ),
  // --- quayside + yard work ---
  hammer: (
    <>
      {/* Head across the top, claw wedge, handle straight down. The first version was an
          angled head on a diagonal shaft, which at 15px was the `edit` pencil. */}
      <path d="M6.6 5.2h11.2a1 1 0 0 1 1 1v2.8a1 1 0 0 1-1 1H6.6z" />
      <path d="M6.6 5.2L3.4 7.6l3.2 2.4z" />
      <path d="M12.2 10.4v10.4" />
    </>
  ),
  ruler: (
    <>
      {/* A set square — the yard's zones are laid OUT, not repaired, so this must not be
          another wrench. Two ticks only: three closed up at 15px. */}
      <path d="M4.4 19.6h15.2L4.4 4.4z" />
      <path d="M7.6 16.6v-2.8M11.4 16.6v-2.8" />
    </>
  ),
  trash: (
    <>
      <path d="M3.8 6.8h16.4" />
      <path d="M9 6.8V4.4h6v2.4" />
      <path d="M5.8 6.8l1 13.2a1 1 0 0 0 1 .9h8.4a1 1 0 0 0 1-.9l1-13.2" />
      <path d="M10.2 10.8v6.4M13.8 10.8v6.4" />
    </>
  ),
  refresh: (
    <>
      <path d="M20.5 12a8.5 8.5 0 1 1-2.9-6.4" />
      <path d="M20.8 4v5.2h-5.2" />
    </>
  ),
  flask: (
    <>
      <path d="M9.5 3.2h5" />
      <path d="M10.2 3.2v6.1L5.4 18.6A1.5 1.5 0 0 0 6.7 20.8h10.6a1.5 1.5 0 0 0 1.3-2.2L13.8 9.3V3.2" />
      <path d="M7.6 15.2h8.8" />
    </>
  ),
  // --- money + analytics ---
  trendUp: (
    <>
      <path d="M3.5 17.5l5.5-5.5 3.5 3.5 6-6" />
      <path d="M14 9.5h4.5V14" />
      <path d="M3.5 20.8h17" />
    </>
  ),
  swap: (
    <>
      {/* Subletting = an exchange: the berth goes out, the owner's share comes back. Drawn
          as two opposed arrows because two clasped hands are indistinguishable at 15px —
          the first attempt read as a plain zigzag. */}
      <path d="M3.6 9.4h14.2" />
      <path d="M15.2 6.6l2.8 2.8-2.8 2.8" />
      <path d="M20.4 15.2H6.2" />
      <path d="M8.8 12.4L6 15.2l2.8 2.8" />
    </>
  ),
  users: (
    <>
      <circle cx="9.2" cy="8.4" r="3.4" />
      <path d="M3.2 20c0-3.3 2.7-5.6 6-5.6s6 2.3 6 5.6" />
      <path d="M15.6 5.4a3.4 3.4 0 0 1 0 6.4" />
      <path d="M17.4 14.8c2 .8 3.4 2.7 3.4 5.2" />
    </>
  ),
  // --- misc signals ---
  signature: (
    <>
      <path d="M3.5 19.5h17" />
      <path d="M5.5 16.4c2.4-1.2 3.2-8.4 5.4-8.4 1.8 0 .8 5.6 2.8 5.6 1.6 0 2.2-3.4 4.2-3.4" />
    </>
  ),
  home: (
    <>
      <path d="M3.8 10.4L12 3.6l8.2 6.8V20a1 1 0 0 1-1 1H4.8a1 1 0 0 1-1-1z" />
      <path d="M9.4 21v-6.2h5.2V21" />
    </>
  ),
  display: (
    <>
      {/* "Follow the system" — a screen, not a phone: this portal lives on an office desktop
          far more than on a handset. */}
      <rect x="2.8" y="3.8" width="18.4" height="12.6" rx="1.7" />
      <path d="M12 16.4v3.8M8.8 20.2h6.4" />
    </>
  ),
  globe: (
    <>
      <circle cx="12" cy="12" r="8.8" />
      <path d="M3.4 12h17.2" />
      <path d="M12 3.2c2.4 2.4 3.6 5.4 3.6 8.8S14.4 18.4 12 20.8c-2.4-2.4-3.6-5.4-3.6-8.8S9.6 5.6 12 3.2z" />
    </>
  ),
  radio: (
    <>
      {/* VHF: a set with an aerial, not a broadcast tower — this labels a channel number. */}
      <rect x="3" y="9.5" width="18" height="11" rx="1.8" />
      <circle cx="8" cy="15" r="2.6" />
      <path d="M14 13h4M14 16.5h4" />
      <path d="M17 9.5l-3-6" />
    </>
  ),
  bulb: (
    <>
      <path d="M9.2 17.4a6 6 0 1 1 5.6 0" />
      <path d="M9.6 17.4h4.8M10.4 20.4h3.2" />
    </>
  ),
  thumbUp: (
    <>
      <path d="M7.5 10.5l3.4-6.6a1.6 1.6 0 0 1 3 .7v4.4h4.4a1.8 1.8 0 0 1 1.8 2.2l-1.4 6a1.8 1.8 0 0 1-1.8 1.4H7.5" />
      <rect x="3.2" y="10.5" width="4.3" height="8.1" rx="1" />
    </>
  ),
  thumbDown: (
    <>
      <path d="M7.5 13.5l3.4 6.6a1.6 1.6 0 0 0 3-.7v-4.4h4.4a1.8 1.8 0 0 0 1.8-2.2l-1.4-6A1.8 1.8 0 0 0 16.9 5.4H7.5" />
      <rect x="3.2" y="5.4" width="4.3" height="8.1" rx="1" />
    </>
  ),
  // --- weather conditions ---
  // One cloud body, reused at the same coordinates in every cloudy state, so the row of
  // forecast cards reads as one family and only the precipitation below it changes.
  cloudSun: (
    <>
      <circle cx="7.4" cy="6.6" r="2.6" />
      <path d="M7.4 1.8v1.2M7.4 10.2v1.2M2.6 6.6h1.2M11 6.6h1.2M4.1 3.3l.9.9M10.7 3.3l-.9.9" />
      <path d="M17.6 19.5a3.6 3.6 0 0 0-.6-7.1 5 5 0 0 0-9.5 1.4 3.2 3.2 0 0 0 .4 5.7z" />
    </>
  ),
  cloudDrizzle: (
    <>
      <path d="M17.4 15.6a3.7 3.7 0 0 0-.7-7.3A5.2 5.2 0 0 0 6.9 9.8 3.3 3.3 0 0 0 7.2 16" />
      <path d="M9.6 18.4v1.4M14.4 18.4v1.4" />
    </>
  ),
  rain: (
    <>
      <path d="M17.4 15.6a3.7 3.7 0 0 0-.7-7.3A5.2 5.2 0 0 0 6.9 9.8 3.3 3.3 0 0 0 7.2 16" />
      <path d="M8.6 18.2l-.8 2.6M12 18.2l-.8 2.6M15.4 18.2l-.8 2.6" />
    </>
  ),
  snow: (
    <>
      <path d="M17.4 15.6a3.7 3.7 0 0 0-.7-7.3A5.2 5.2 0 0 0 6.9 9.8 3.3 3.3 0 0 0 7.2 16" />
      <path d="M9 19.4h.02M12 21h.02M15 19.4h.02" />
      <path d="M8.2 19.4h1.6M11.2 21h1.6M14.2 19.4h1.6" />
    </>
  ),
  storm: (
    <>
      <path d="M17.4 14.4a3.7 3.7 0 0 0-.7-7.3A5.2 5.2 0 0 0 6.9 8.6 3.3 3.3 0 0 0 7.2 14.8" />
      <path d="M13.2 16.2l-2.8 3.4h2.6l-1 3.2" />
    </>
  ),
  fog: (
    <>
      <path d="M17.2 12.6a3.6 3.6 0 0 0-.7-7.1A5.1 5.1 0 0 0 7 6.9a3.2 3.2 0 0 0 .3 6" />
      <path d="M4.4 16.2h15.2M6.6 19.4h11" />
    </>
  ),
};

export function Icon({
  name,
  size = 18,
  className,
}: {
  name: IconName;
  size?: number;
  /** Extra class, e.g. for a muted or accent colour. Colour comes from `currentColor`. */
  className?: string;
}) {
  return (
    <svg
      className={className ? `icon ${className}` : "icon"}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      // Decorative: every icon in this app sits next to its own text label, so announcing
      // it would just read the label twice.
      aria-hidden="true"
      focusable="false"
    >
      {PATHS[name]}
    </svg>
  );
}

/** The boat marker for Leaflet maps, as an SVG *string*.
 *
 *  `L.divIcon` takes raw HTML, not JSX, so this cannot be the <Icon> component — and it
 *  should not be: over a busy chart a 1.75px outline disappears against coastline and
 *  depth contours, so the marker is a FILLED silhouette with a white keyline, which reads
 *  at a glance on any tile. Same hull-and-sail geometry as `sailboat`, deliberately, so
 *  the boat on the map and the boat in the sidebar are recognisably the same object.
 */
export const PIN_MARKER_SVG = `<svg viewBox="0 0 24 24" width="26" height="26" aria-hidden="true">
  <g fill="#c1121f" stroke="#fff" stroke-width="1.1" stroke-linejoin="round">
    <path d="M12 2.2c-3.6 0-6.5 2.9-6.5 6.5 0 4.8 6.5 12.6 6.5 12.6s6.5-7.8 6.5-12.6c0-3.6-2.9-6.5-6.5-6.5z"/>
    <circle cx="12" cy="8.7" r="2.4" fill="#fff" stroke="none"/>
  </g>
</svg>`;

export const BOAT_MARKER_SVG = `<svg viewBox="0 0 24 24" width="26" height="26" aria-hidden="true">
  <g fill="#0b2545" stroke="#fff" stroke-width="1.1" stroke-linejoin="round">
    <path d="M12.2 13.6V3.2l7 10.4z"/>
    <path d="M10.4 13.6V5.6L5.6 13.6z"/>
    <path d="M3.2 15.4h17.6l-2.4 4.2H5.6z"/>
  </g>
</svg>`;
