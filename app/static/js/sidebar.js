/**
 * Sidebar behaviour: collapsible multi-column sub-menus, plus the mobile drawer.
 *
 * COLUMN RULE -- the server-side half of this lives in `app/navigation.py`
 * (`submenu_columns`), and the two must agree or the layout jumps on first
 * resize:
 *
 *     rows <= ROW_THRESHOLD  ->  1 column
 *     otherwise              ->  min(MAX_COLUMNS, ceil(rows / ROWS_PER_COLUMN))
 *
 * So a sub-menu only splits once its row count *exceeds* five.
 *
 * SCOPE -- the only elements this file ever re-classes are
 * `[data-nav-submenu-list]` nodes. Level-one controls (`[data-nav-level="1"]`)
 * are read for their aria state and otherwise left alone; `layoutSubmenu` bails
 * out if it is ever handed something inside one, so the top level of the
 * navigation cannot be reflowed from here.
 */
(function () {
  'use strict';

  /* Defaults. The nav root carries the authoritative values as data-* so the
     Python constants stay the single source of truth at runtime. */
  var ROW_THRESHOLD = 5;
  var ROWS_PER_COLUMN = 5;
  var MAX_COLUMNS = 3;

  var DESKTOP_QUERY = '(min-width: 1024px)'; /* Tailwind's `lg` breakpoint. */
  var STORAGE_KEY = 'vpshosting.sidebar.open';

  var nav = document.querySelector('[data-nav]');
  var aside = document.querySelector('[data-sidebar]');
  var overlay = document.querySelector('[data-sidebar-overlay]');
  var openButton = document.querySelector('[data-sidebar-toggle]');
  var closeButton = document.querySelector('[data-sidebar-close]');
  var desktop = window.matchMedia(DESKTOP_QUERY);

  /* -------------------------------------------------------------------- */
  /* Column arithmetic                                                     */
  /* -------------------------------------------------------------------- */
  function readConfig(root) {
    function attr(name, fallback) {
      var value = parseInt(root.getAttribute(name), 10);
      return isNaN(value) || value < 1 ? fallback : value;
    }
    return {
      threshold: attr('data-nav-row-threshold', ROW_THRESHOLD),
      perColumn: attr('data-nav-rows-per-column', ROWS_PER_COLUMN),
      maxColumns: attr('data-nav-max-columns', MAX_COLUMNS)
    };
  }

  function columnsFor(rows, config) {
    if (rows <= config.threshold) {
      return 1;
    }
    return Math.min(config.maxColumns, Math.ceil(rows / config.perColumn));
  }

  /* Tailwind ships grid-rows-1..6; anything taller needs the arbitrary form. */
  function rowClass(rows) {
    return rows <= 6
      ? 'grid-rows-' + rows
      : 'grid-rows-[repeat(' + rows + ',minmax(0,min-content))]';
  }

  function isGridClass(name) {
    return /^grid-(cols|rows|flow)-/.test(name);
  }

  /**
   * Apply the column layout to one sub-menu list.
   * `wide` is false below the desktop breakpoint, where a single column always
   * wins regardless of row count -- two columns in a 288px drawer is unreadable.
   */
  function layoutSubmenu(list, config, wide) {
    /* Hard guard on the "never touch level one" rule. */
    if (!list || list.closest('[data-nav-level="1"]')) {
      return;
    }

    var panel = list.closest('[data-nav-submenu]');
    var declared = panel && parseInt(panel.getAttribute('data-nav-rows'), 10);
    var rows = declared > 0 ? declared : list.children.length;
    if (!rows) {
      return;
    }

    var columns = wide ? columnsFor(rows, config) : 1;

    /* Strip whatever was applied last time -- by the server on first paint, or
       by this function on a previous resize -- then re-add from scratch. */
    list.className = list.className
      .split(/\s+/)
      .filter(function (name) {
        return name && !isGridClass(name);
      })
      .join(' ');

    list.classList.add('grid-cols-' + columns);
    if (columns > 1) {
      /* Column flow fills each column top to bottom before starting the next,
         so the reading order matches a newspaper column rather than zig-zagging
         across the rows. */
      list.classList.add('grid-flow-col', rowClass(Math.ceil(rows / columns)));
    }
    list.setAttribute('data-nav-columns', String(columns));
  }

  function relayout() {
    if (!nav) {
      return;
    }
    var config = readConfig(nav);
    var wide = desktop.matches;
    nav.querySelectorAll('[data-nav-submenu-list]').forEach(function (list) {
      layoutSubmenu(list, config, wide);
    });
  }

  /* -------------------------------------------------------------------- */
  /* Expand / collapse                                                     */
  /* -------------------------------------------------------------------- */
  function groups() {
    return nav ? Array.prototype.slice.call(nav.querySelectorAll('[data-nav-group]')) : [];
  }

  function isExpanded(group) {
    var toggle = group.querySelector('[data-nav-toggle]');
    return !!toggle && toggle.getAttribute('aria-expanded') === 'true';
  }

  function setExpanded(group, expanded) {
    var toggle = group.querySelector('[data-nav-toggle]');
    var panel = group.querySelector('[data-nav-submenu]');
    if (!toggle || !panel) {
      return;
    }
    toggle.setAttribute('aria-expanded', expanded ? 'true' : 'false');

    /* The height animation is a grid-template-rows interpolation from 0fr to
       1fr, which needs no measurement and survives content changes. */
    panel.classList.toggle('grid-rows-[1fr]', expanded);
    panel.classList.toggle('grid-rows-[0fr]', !expanded);

    /* `inert` keeps a collapsed sub-menu out of the tab order and off the
       accessibility tree; without it the links stay focusable at zero height. */
    if (expanded) {
      panel.removeAttribute('inert');
    } else {
      panel.setAttribute('inert', '');
    }
  }

  function persist() {
    var open = groups()
      .filter(isExpanded)
      .map(function (group) {
        return group.getAttribute('data-nav-key');
      });
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(open));
    } catch (error) {
      /* Private browsing or a full quota: open state is a nicety, not data. */
    }
  }

  function restore() {
    var stored = [];
    try {
      stored = JSON.parse(window.localStorage.getItem(STORAGE_KEY)) || [];
    } catch (error) {
      stored = [];
    }
    if (!Array.isArray(stored)) {
      return;
    }
    groups().forEach(function (group) {
      /* The server already expanded the group holding the current page; never
         collapse that one on the strength of a stale localStorage entry. */
      if (isExpanded(group)) {
        return;
      }
      if (stored.indexOf(group.getAttribute('data-nav-key')) !== -1) {
        setExpanded(group, true);
      }
    });
  }

  function onToggleClick(event) {
    var toggle = event.target.closest('[data-nav-toggle]');
    if (!toggle || !nav.contains(toggle)) {
      return;
    }
    var group = toggle.closest('[data-nav-group]');
    if (!group) {
      return;
    }
    var opening = !isExpanded(group);

    /* Accordion: one sub-menu open at a time keeps the sidebar from growing
       past the viewport once several groups are expanded. */
    if (opening) {
      groups().forEach(function (other) {
        if (other !== group) {
          setExpanded(other, false);
        }
      });
    }
    setExpanded(group, opening);
    persist();
  }

  function onNavKeydown(event) {
    if (event.key !== 'Escape') {
      return;
    }
    var group = event.target.closest('[data-nav-group]');
    if (group && isExpanded(group)) {
      setExpanded(group, false);
      persist();
      var toggle = group.querySelector('[data-nav-toggle]');
      if (toggle) {
        toggle.focus();
      }
    }
  }

  /* -------------------------------------------------------------------- */
  /* Mobile drawer                                                         */
  /* -------------------------------------------------------------------- */
  function drawerOpen() {
    return !!aside && !aside.classList.contains('-translate-x-full');
  }

  function setDrawer(open) {
    if (!aside) {
      return;
    }
    aside.classList.toggle('-translate-x-full', !open);
    if (overlay) {
      overlay.toggleAttribute('hidden', !open);
    }
    if (openButton) {
      openButton.setAttribute('aria-expanded', open ? 'true' : 'false');
    }
    /* Stop the page behind the drawer from scrolling with it. */
    document.documentElement.classList.toggle('overflow-hidden', open);

    if (open && closeButton) {
      closeButton.focus();
    } else if (!open && openButton && document.activeElement !== document.body) {
      openButton.focus();
    }
  }

  function onDocumentKeydown(event) {
    if (event.key === 'Escape' && drawerOpen() && !desktop.matches) {
      setDrawer(false);
    }
  }

  /* -------------------------------------------------------------------- */
  /* Wiring                                                                */
  /* -------------------------------------------------------------------- */
  if (nav) {
    nav.addEventListener('click', onToggleClick);
    nav.addEventListener('keydown', onNavKeydown);
    restore();
    relayout();
  }

  if (openButton) {
    openButton.addEventListener('click', function () {
      setDrawer(true);
    });
  }
  if (closeButton) {
    closeButton.addEventListener('click', function () {
      setDrawer(false);
    });
  }
  if (overlay) {
    overlay.addEventListener('click', function () {
      setDrawer(false);
    });
  }
  document.addEventListener('keydown', onDocumentKeydown);

  /* Following a sub-menu link inside the drawer should close it, otherwise the
     new page is hidden behind the panel on mobile. */
  if (aside) {
    aside.addEventListener('click', function (event) {
      var link = event.target.closest('a[href]');
      if (link && !desktop.matches && drawerOpen()) {
        setDrawer(false);
      }
    });
  }

  /* Crossing the breakpoint changes both the column count and whether the
     drawer applies at all. Coalesce into one frame so a slow drag does not
     thrash the class lists. */
  var pending = false;
  function onViewportChange() {
    if (pending) {
      return;
    }
    pending = true;
    window.requestAnimationFrame(function () {
      pending = false;
      relayout();
      if (desktop.matches && drawerOpen()) {
        document.documentElement.classList.remove('overflow-hidden');
        if (overlay) {
          overlay.setAttribute('hidden', '');
        }
      }
    });
  }

  window.addEventListener('resize', onViewportChange);
  if (typeof desktop.addEventListener === 'function') {
    desktop.addEventListener('change', onViewportChange);
  }

  /* Exposed for the console and for future pages that inject nav markup. */
  window.VpsHostingSidebar = {
    relayout: relayout,
    columnsFor: columnsFor,
    setDrawer: setDrawer
  };
})();
