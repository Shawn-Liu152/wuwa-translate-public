(() => {
  "use strict";

  const STORAGE_KEY = "phoebe-web-pet-v2";
  const FRAME_WIDTH = 192;
  const FRAME_HEIGHT = 208;
  const LOOP_MS = 1100;
  const PET_CATALOG = {
    feibi: { name: "菲比", summon: "菲", src: "/static/assets/pets/feibi.webp" },
    coletta: { name: "珂莱塔", summon: "珂", src: "/static/assets/pets/coletta.webp" },
    phrolova: { name: "弗洛洛", summon: "弗", src: "/static/assets/pets/phrolova.webp" },
    taffy: { name: "塔菲", summon: "塔", src: "/static/assets/pets/taffy.webp" },
  };
  const STATE_INFO = {
    idle: { row: 0, frames: 6 },
    "run-right": { row: 1, frames: 8 },
    "run-left": { row: 2, frames: 8 },
    waving: { row: 3, frames: 4 },
    jumping: { row: 4, frames: 5 },
    failed: { row: 5, frames: 8 },
    waiting: { row: 6, frames: 6 },
    running: { row: 7, frames: 6 },
    review: { row: 8, frames: 6 },
  };
  const TRANSIENT_MS = {
    waving: 1650,
    jumping: 1450,
  };
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
  const smallScreen = matchMedia("(max-width: 620px)");

  const readSettings = () => {
    try {
      return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
    } catch {
      return {};
    }
  };

  const settings = readSettings();
  let currentPetId = PET_CATALOG[settings.petId] ? settings.petId : "feibi";
  const currentPet = () => PET_CATALOG[currentPetId];
  const pet = document.createElement("div");
  pet.className = "web-pet";
  pet.dataset.state = "idle";
  pet.dataset.pet = currentPetId;
  pet.innerHTML = `
    <div class="web-pet__tools" aria-label="桌面宠物控制">
      <button type="button" class="web-pet__tool" data-pet-action="waving" title="挥手" aria-label="让宠物挥手">✦</button>
      <button type="button" class="web-pet__tool" data-pet-action="jumping" title="跳跃" aria-label="让宠物跳一下">↑</button>
      <button type="button" class="web-pet__tool" data-pet-action="roam" title="空闲漫游" aria-label="切换宠物空闲漫游" aria-pressed="false">↔</button>
      <button type="button" class="web-pet__tool" data-pet-action="hide" title="收起" aria-label="暂时收起宠物">×</button>
    </div>
    <button type="button" class="web-pet__sprite"></button>
  `;

  const summon = document.createElement("button");
  summon.type = "button";
  summon.className = "web-pet-summon";
  summon.textContent = currentPet().summon;
  summon.title = `叫回${currentPet().name}`;
  summon.setAttribute("aria-label", `叫回${currentPet().name}宠物`);
  document.body.append(pet, summon);

  const sprite = pet.querySelector(".web-pet__sprite");
  const tools = pet.querySelector(".web-pet__tools");
  const roamButton = pet.querySelector('[data-pet-action="roam"]');
  sprite.setAttribute(
    "aria-label",
    `${currentPet().name}宠物：单击挥手，双击跳跃，可以拖动`,
  );
  const canvas = document.createElement("canvas");
  canvas.className = "web-pet__canvas";
  sprite.append(canvas);

  const image = new Image();
  image.decoding = "async";
  image.src = currentPet().src;

  let context = null;
  let drawWidth = 96;
  let drawHeight = 104;
  let visualState = "idle";
  let activityState = "idle";
  let frame = 0;
  let drawnFrame = -1;
  let drawnRow = -1;
  let lastStep = performance.now();
  let animationFrame = 0;
  let frameTimer = 0;
  let transientTimer = 0;
  let clickTimer = 0;
  let roamTimer = 0;
  let roamFrame = 0;
  let drag = null;
  let motionReducedByApp = false;

  const clamp = (value, min, max) => Math.min(Math.max(value, min), max);
  const normalizeState = (state) => STATE_INFO[state] ? state : "idle";
  const motionReduced = () => reducedMotion.matches || motionReducedByApp;

  const configureCanvas = () => {
    const scale = smallScreen.matches ? 0.42 : 0.5;
    drawWidth = Math.round(FRAME_WIDTH * scale);
    drawHeight = Math.round(FRAME_HEIGHT * scale);
    const ratio = Math.max(1, window.devicePixelRatio || 1);

    canvas.width = Math.round(drawWidth * ratio);
    canvas.height = Math.round(drawHeight * ratio);
    canvas.style.width = `${drawWidth}px`;
    canvas.style.height = `${drawHeight}px`;
    pet.style.setProperty("--web-pet-frame-width", `${drawWidth}px`);
    pet.style.setProperty("--web-pet-frame-height", `${drawHeight}px`);
    context = canvas.getContext("2d", { alpha: true, willReadFrequently: false });
    context?.setTransform(ratio, 0, 0, ratio, 0, 0);
    if (context) context.imageSmoothingEnabled = false;
    drawnFrame = -1;
    drawnRow = -1;
  };

  const applyState = (state) => {
    const next = normalizeState(state);
    if (next === visualState) return;
    visualState = next;
    pet.dataset.state = next;
    frame = 0;
    drawnFrame = -1;
    drawnRow = -1;
    lastStep = performance.now();
    scheduleAnimation();
  };

  const clearRoam = () => {
    clearTimeout(roamTimer);
    cancelAnimationFrame(roamFrame);
    roamTimer = 0;
    roamFrame = 0;
  };

  const scheduleRoam = () => {
    clearRoam();
    if (!settings.roam || pet.hidden || motionReduced() || activityState !== "idle" || drag) return;
    const random = Math.max(0.001, 1 - Math.random());
    const dwell = clamp(-Math.log(random) * 4200, 1500, 13000);
    roamTimer = window.setTimeout(beginRoam, dwell);
  };

  const returnToActivity = () => {
    applyState(activityState);
    if (activityState === "idle") scheduleRoam();
  };

  const playTransient = (state, duration = TRANSIENT_MS[state] || 1600) => {
    clearTimeout(transientTimer);
    clearRoam();
    applyState(state);
    transientTimer = window.setTimeout(returnToActivity, duration);
  };

  const setActivity = (state) => {
    clearTimeout(transientTimer);
    clearRoam();
    activityState = normalizeState(state);
    applyState(activityState);
    if (activityState === "idle") scheduleRoam();
  };

  const beginRoam = () => {
    if (!settings.roam || activityState !== "idle" || drag || pet.hidden) return;
    if (Math.random() < 0.62) {
      scheduleRoam();
      return;
    }

    const rect = pet.getBoundingClientRect();
    const minX = 8;
    const maxX = Math.max(minX, window.innerWidth - rect.width - 8);
    const roomLeft = rect.left - minX;
    const roomRight = maxX - rect.left;
    const goRight = Math.random() < 0.85 ? roomRight >= roomLeft : roomRight < roomLeft;
    const room = Math.max(0, goRight ? roomRight : roomLeft);
    const minDistance = Math.min(room, Math.max((maxX - minX) * 0.45, 110));
    const distance = minDistance + Math.random() * Math.max(0, room - minDistance);
    const target = clamp(rect.left + (goRight ? distance : -distance), minX, maxX);
    const speed = (drawWidth * 0.8) / (LOOP_MS / 1000);
    let last = performance.now();
    applyState(goRight ? "run-right" : "run-left");

    const step = (now) => {
      if (activityState !== "idle" || drag || pet.hidden) {
        returnToActivity();
        return;
      }
      const current = pet.getBoundingClientRect().left;
      const remaining = target - current;
      const distanceThisFrame = speed * Math.min(0.05, (now - last) / 1000);
      last = now;
      if (Math.abs(remaining) <= Math.max(1.5, distanceThisFrame)) {
        pet.style.left = `${target}px`;
        saveSettings();
        returnToActivity();
        return;
      }
      pet.style.left = `${current + Math.sign(remaining) * distanceThisFrame}px`;
      roamFrame = requestAnimationFrame(step);
    };

    roamFrame = requestAnimationFrame(step);
  };

  const stopAnimation = () => {
    clearTimeout(frameTimer);
    cancelAnimationFrame(animationFrame);
    frameTimer = 0;
    animationFrame = 0;
  };

  const scheduleAnimation = (delay = 0) => {
    stopAnimation();
    if (document.hidden || pet.hidden) return;
    frameTimer = window.setTimeout(() => {
      frameTimer = 0;
      animationFrame = requestAnimationFrame(render);
    }, Math.max(0, delay));
  };

  const render = (now) => {
    animationFrame = 0;
    const info = STATE_INFO[visualState];
    const count = motionReduced() ? 1 : info.frames;
    const stepMs = LOOP_MS / count;
    if (now - lastStep >= stepMs) {
      frame = (frame + Math.max(1, Math.floor((now - lastStep) / stepMs))) % count;
      lastStep = now;
    }

    if (
      context &&
      image.complete &&
      image.naturalWidth > 0 &&
      (frame !== drawnFrame || info.row !== drawnRow)
    ) {
      context.clearRect(0, 0, drawWidth, drawHeight);
      context.imageSmoothingEnabled = false;
      context.drawImage(
        image,
        frame * FRAME_WIDTH,
        info.row * FRAME_HEIGHT,
        FRAME_WIDTH,
        FRAME_HEIGHT,
        0,
        0,
        drawWidth,
        drawHeight,
      );
      drawnFrame = frame;
      drawnRow = info.row;
    }

    if (!motionReduced()) {
      const elapsed = performance.now() - lastStep;
      scheduleAnimation(Math.max(16, stepMs - elapsed));
    }
  };

  const saveSettings = (extra = {}) => {
    const rect = pet.getBoundingClientRect();
    const next = {
      hidden: pet.hidden,
      left: pet.hidden ? settings.left : Math.round(rect.left),
      top: pet.hidden ? settings.top : Math.round(rect.top),
      roam: Boolean(settings.roam),
      petId: currentPetId,
      ...extra,
    };
    Object.assign(settings, next);
    localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  };

  const switchPet = (petId) => {
    if (!PET_CATALOG[petId] || petId === currentPetId) return;
    currentPetId = petId;
    settings.petId = petId;
    pet.dataset.pet = petId;
    summon.textContent = currentPet().summon;
    summon.title = `叫回${currentPet().name}`;
    summon.setAttribute("aria-label", `叫回${currentPet().name}宠物`);
    sprite.setAttribute(
      "aria-label",
      `${currentPet().name}宠物：单击挥手，双击跳跃，可以拖动`,
    );
    frame = 0;
    drawnFrame = -1;
    drawnRow = -1;
    image.src = currentPet().src;
    saveSettings({ petId });
    scheduleAnimation();
  };

  const fitToolsToViewport = () => {
    if (pet.hidden || !tools.offsetWidth) return;
    tools.style.setProperty("--web-pet-tools-shift", "0px");
    const petRect = pet.getBoundingClientRect();
    const naturalLeft = petRect.left + (petRect.width - tools.offsetWidth) / 2;
    const fittedLeft = clamp(
      naturalLeft,
      8,
      Math.max(8, window.innerWidth - tools.offsetWidth - 8),
    );
    tools.style.setProperty(
      "--web-pet-tools-shift",
      `${Math.round(fittedLeft - naturalLeft)}px`,
    );
  };

  const fitToViewport = () => {
    const rect = pet.getBoundingClientRect();
    const left = clamp(
      Number.isFinite(settings.left) ? settings.left : rect.left,
      8,
      Math.max(8, window.innerWidth - rect.width - 8),
    );
    const top = clamp(
      Number.isFinite(settings.top) ? settings.top : rect.top,
      8,
      Math.max(8, window.innerHeight - rect.height - 8),
    );
    pet.style.left = `${left}px`;
    pet.style.top = `${top}px`;
    fitToolsToViewport();
  };

  const setHidden = (hidden) => {
    if (hidden) saveSettings({ hidden: true });
    pet.hidden = hidden;
    summon.hidden = !hidden;
    clearRoam();
    if (hidden) {
      stopAnimation();
    } else {
      fitToViewport();
      saveSettings({ hidden: false });
      playTransient("waving");
      scheduleAnimation();
      sprite.focus({ preventScroll: true });
    }
  };

  pet.querySelectorAll("[data-pet-action]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const action = button.dataset.petAction;
      if (action === "hide") {
        setHidden(true);
      } else if (action === "roam") {
        settings.roam = !settings.roam;
        roamButton.setAttribute("aria-pressed", String(settings.roam));
        saveSettings({ roam: settings.roam });
        if (settings.roam) scheduleRoam();
        else {
          clearRoam();
          returnToActivity();
        }
      } else {
        playTransient(action);
      }
    });
  });

  summon.addEventListener("click", () => setHidden(false));

  sprite.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    clearRoam();
    const rect = pet.getBoundingClientRect();
    drag = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      originLeft: rect.left,
      originTop: rect.top,
      moved: false,
      lastX: event.clientX,
    };
    sprite.setPointerCapture(event.pointerId);
    pet.classList.add("is-dragging");
    event.preventDefault();
  });

  sprite.addEventListener("pointermove", (event) => {
    if (!drag || drag.pointerId !== event.pointerId) return;
    const dx = event.clientX - drag.startX;
    const dy = event.clientY - drag.startY;
    if (Math.abs(dx) + Math.abs(dy) > 6) drag.moved = true;
    if (!drag.moved) return;
    const left = clamp(
      drag.originLeft + dx,
      8,
      Math.max(8, window.innerWidth - pet.offsetWidth - 8),
    );
    const top = clamp(
      drag.originTop + dy,
      8,
      Math.max(8, window.innerHeight - pet.offsetHeight - 8),
    );
    pet.style.left = `${left}px`;
    pet.style.top = `${top}px`;
    fitToolsToViewport();
    applyState(event.clientX >= drag.lastX ? "run-right" : "run-left");
    drag.lastX = event.clientX;
  });

  const finishDrag = (event) => {
    if (!drag || drag.pointerId !== event.pointerId) return;
    const moved = drag.moved;
    drag = null;
    pet.classList.remove("is-dragging");
    if (sprite.hasPointerCapture(event.pointerId)) sprite.releasePointerCapture(event.pointerId);
    if (moved) {
      saveSettings();
      returnToActivity();
      return;
    }
    clearTimeout(clickTimer);
    clickTimer = window.setTimeout(() => playTransient("waving"), 220);
  };

  sprite.addEventListener("pointerup", finishDrag);
  sprite.addEventListener("pointercancel", finishDrag);
  sprite.addEventListener("dblclick", (event) => {
    event.preventDefault();
    clearTimeout(clickTimer);
    playTransient("jumping");
  });

  window.addEventListener("phoebe-pet", (event) => {
    if (event.detail?.transient) {
      activityState = "idle";
      playTransient(event.detail?.state);
      return;
    }
    setActivity(event.detail?.state);
  });
  window.addEventListener("phoebe-pet-settings", (event) => {
    const detail = event.detail || {};
    if (typeof detail.petId === "string") {
      switchPet(detail.petId);
    }
    if (typeof detail.motionReduced === "boolean") {
      motionReducedByApp = detail.motionReduced;
      frame = 0;
      drawnFrame = -1;
      if (motionReduced()) clearRoam();
      else scheduleRoam();
      scheduleAnimation();
    }
    if (typeof detail.roam === "boolean") {
      settings.roam = detail.roam;
      roamButton.setAttribute("aria-pressed", String(settings.roam));
      saveSettings({ roam: settings.roam });
      if (settings.roam) scheduleRoam();
      else {
        clearRoam();
        returnToActivity();
      }
    }
    if (typeof detail.enabled === "boolean") {
      const shouldHide = !detail.enabled;
      if (pet.hidden !== shouldHide) setHidden(shouldHide);
    }
  });

  const onViewportChange = () => {
    const rect = pet.getBoundingClientRect();
    settings.left = rect.left;
    settings.top = rect.top;
    configureCanvas();
    fitToViewport();
    saveSettings();
  };
  window.addEventListener("resize", onViewportChange);
  smallScreen.addEventListener?.("change", onViewportChange);
  reducedMotion.addEventListener?.("change", () => {
    drawnFrame = -1;
    frame = 0;
    if (motionReduced()) clearRoam();
    else scheduleRoam();
    scheduleAnimation();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stopAnimation();
      clearRoam();
      return;
    }
    drawnFrame = -1;
    lastStep = performance.now();
    scheduleAnimation();
    scheduleRoam();
  });

  settings.roam = Boolean(settings.roam);
  roamButton.setAttribute("aria-pressed", String(settings.roam));
  pet.hidden = Boolean(settings.hidden);
  summon.hidden = !pet.hidden;
  configureCanvas();
  requestAnimationFrame(() => {
    fitToViewport();
    scheduleRoam();
  });
  image.addEventListener("load", () => {
    drawnFrame = -1;
    scheduleAnimation();
  });
  image.addEventListener("error", () => {
    pet.hidden = true;
    summon.hidden = false;
    summon.title = "菲比精灵图加载失败";
  });
  scheduleAnimation();

  window.addEventListener("pagehide", () => {
    stopAnimation();
    clearRoam();
  }, { once: true });
})();
