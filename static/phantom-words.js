/** Lightweight, fixed-resolution phantom-word newspaper effect. */
(() => {
  if (customElements.get("phantom-words")) return;

  const WIDTH = 900;
  const HEIGHT = 1234;
  const VOCABULARY = "signal error trace offset ghost buffer lag static echo void memory render packet noise repeat system flicker phase decode fault cache drift process replace phantom mirror frame overflow glitch rewrite fragment shadow latency frozen reload misprint syntax bypass silent vanished observed".split(" ");
  const TEXT_ZONES = [
    { x: [590, 760], y: [195, 325], size: [26, 36] },
    { x: [95, 705], y: [140, 150], size: [14, 18] },
    { x: [565, 840], y: [370, 790], size: [10, 15] },
    { x: [44, 232], y: [555, 1000], size: [10, 15] },
    { x: [246, 545], y: [555, 775], size: [10, 15] },
    { x: [150, 230], y: [1015, 1180], size: [9, 13] },
    { x: [574, 655], y: [825, 1155], size: [9, 13] },
  ];
  const random = (min, max) => min + Math.random() * (max - min);
  const pick = (items) => items[Math.floor(Math.random() * items.length)];
  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));

  class PhantomWords extends HTMLElement {
    static get observedAttributes() {
      return ["rate", "intensity", "words", "src"];
    }

    constructor() {
      super();
      this.attachShadow({ mode: "open" });
      this.shadowRoot.innerHTML = `
        <style>
          :host {
            display: block;
            position: relative;
            contain: layout paint;
            isolation: isolate;
            aspect-ratio: 1312 / 1800;
            overflow: hidden;
            background: #ece9e1;
          }
          img, canvas {
            position: absolute;
            inset: 0;
            display: block;
            width: 100%;
            height: 100%;
          }
          img { object-fit: fill; filter: brightness(1.1) contrast(1.07) sepia(.035); }
          canvas { z-index: 1; pointer-events: none; }
        </style>
        <img part="paper" loading="eager" decoding="async" alt="" aria-hidden="true" />
        <canvas part="glitch" aria-hidden="true"></canvas>
      `;
      this.paper = this.shadowRoot.querySelector("img");
      this.canvas = this.shadowRoot.querySelector("canvas");
      this.ctx = this.canvas.getContext("2d", { alpha: true });
      this.canvas.width = WIDTH;
      this.canvas.height = HEIGHT;
      this.active = false;
      this.visible = false;
      this.timer = 0;
      this.previous = 0;
      this.spawnAccumulator = 0;
      this.items = [];
      this.elapsed = 0;
      this.onVisibility = () => this.sync();
      this.onFrame = this.onFrame.bind(this);
    }

    connectedCallback() {
      this.paper.src = this.getAttribute("src") || "/assets/guardian-moon-front-page.jpg";
      this.observer = new IntersectionObserver((entries) => {
        this.visible = entries[0]?.isIntersecting ?? false;
        this.sync();
      }, { threshold: .02 });
      this.observer.observe(this);
      document.addEventListener("visibilitychange", this.onVisibility);
      this.motion = window.matchMedia("(prefers-reduced-motion: reduce)");
      this.onMotion = () => this.sync();
      this.motion.addEventListener?.("change", this.onMotion);
      this.sync();
    }

    disconnectedCallback() {
      this.observer?.disconnect();
      document.removeEventListener("visibilitychange", this.onVisibility);
      this.motion?.removeEventListener?.("change", this.onMotion);
      clearTimeout(this.timer);
      this.timer = 0;
    }

    attributeChangedCallback(name, _oldValue, newValue) {
      if (name === "words") {
        this.customWords = (newValue || "").split(",").map((word) => word.trim()).filter(Boolean).slice(0, 100).filter((word) => word.length <= 20);
      }
      if (name === "src" && this.paper) this.paper.src = newValue || "/assets/guardian-moon-front-page.jpg";
    }

    get rate() {
      return clamp(Number(this.getAttribute("rate") || "8"), 0, 24);
    }

    get intensity() {
      return clamp(Number(this.getAttribute("intensity") || ".65"), 0, 1);
    }

    sync() {
      const shouldRun = this.isConnected && this.visible && !document.hidden && !this.motion?.matches && !this.hasAttribute("paused");
      if (shouldRun && !this.active) {
        this.active = true;
        this.previous = 0;
        this.timer = setTimeout(() => this.onFrame(performance.now()), 50);
      } else if (!shouldRun && this.active) {
        this.active = false;
        clearTimeout(this.timer);
        this.timer = 0;
        this.ctx.clearRect(0, 0, WIDTH, HEIGHT);
      }
    }

    spawn() {
      const zone = pick(TEXT_ZONES);
      const vocabulary = this.customWords?.length ? this.customWords : VOCABULARY;
      this.items.push({
        x: random(...zone.x),
        y: random(...zone.y),
        age: 0,
        life: random(1, 1.9),
        text: pick(vocabulary),
        alternate: pick(vocabulary),
        seed: random(0, 100),
        size: random(...zone.size),
      });
      if (this.items.length > 28) this.items.shift();
    }

    step(delta) {
      this.elapsed += delta;
      this.spawnAccumulator += delta * this.rate;
      while (this.spawnAccumulator >= 1) {
        this.spawn();
        this.spawnAccumulator -= 1;
      }
      for (let index = this.items.length - 1; index >= 0; index -= 1) {
        this.items[index].age += delta;
        if (this.items[index].age >= this.items[index].life) this.items.splice(index, 1);
      }
    }

    draw() {
      const ctx = this.ctx;
      const power = this.intensity;
      ctx.clearRect(0, 0, WIDTH, HEIGHT);
      for (const item of this.items) {
        const progress = item.age / item.life;
        const opacity = Math.min(1, progress * 12, (1 - progress) * 9) * (.78 + power * .18);
        const glitching = Math.sin((this.elapsed + item.seed) * 25) > .62;
        const x = item.x + (glitching ? Math.sin(this.elapsed * 44 + item.seed) * power * 3 : 0);
        const y = item.y + (glitching ? Math.cos(this.elapsed * 31 + item.seed) * power : 0);
        const word = glitching && Math.sin(this.elapsed * 55 + item.seed) > .6 ? item.alternate : item.text;
        ctx.font = `bold ${item.size}px Georgia, serif`;
        const width = ctx.measureText(word).width;
        ctx.globalAlpha = opacity;
        ctx.fillStyle = "#ffffff";
        ctx.fillRect(x - 2, y - item.size, width + 5, item.size + 3);
        ctx.globalAlpha = opacity * .12 * power;
        ctx.fillStyle = "#727272";
        ctx.fillText(word, x + 1.5 + power * 2, y);
        ctx.globalAlpha = opacity;
        ctx.fillStyle = "#080808";
        ctx.fillText(word, x, y);
      }
      ctx.globalAlpha = 1;
    }

    onFrame(timestamp) {
      if (!this.active) return;
      const delta = this.previous ? Math.min((timestamp - this.previous) / 1000, .1) : .05;
      this.previous = timestamp;
      this.step(delta);
      this.draw();
      this.timer = setTimeout(() => this.onFrame(performance.now()), 50);
    }

    pause() {
      this.setAttribute("paused", "");
      this.sync();
    }

    play() {
      this.removeAttribute("paused");
      this.sync();
    }
  }

  customElements.define("phantom-words", PhantomWords);
})();
