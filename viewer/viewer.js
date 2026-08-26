/* codex_auto3d preview harness.
 *
 * Runs the generated img2threejs factory in a plain Three.js scene and exposes the capture
 * contract the forge render bridge expects:
 *
 *   window.__IMG2THREEJS_READY__ = true
 *   window.__IMG2THREEJS_CAPTURE__.setCamera({ azimuthDegrees, elevationDegrees, ... })
 *   window.__IMG2THREEJS_CAPTURE__.capturePass({ passId, mode })
 *   window.__IMG2THREEJS_EXPORT_MESHES__({ maxTriangles })
 *
 * Camera convention (shared with frame<Name>Camera and forge/_shared/chirality.py): azimuth 0
 * looks at the subject's front (+Z); positive azimuth moves the camera toward the subject's own
 * left (+X); elevation is degrees above the subject's mid-height.
 *
 * In interactive mode (no ?capture=1) the page adds orbit controls, view buttons and a
 * turntable toggle. In capture mode everything is deterministic: no damping, no auto-rotate,
 * device pixel ratio 1, fixed background.
 */
(function () {
  const CONFIG = window.__AUTO3D_CONFIG__ || {};
  const THREE = window.__AUTO3D_THREE__;
  const FACTORY = window.__AUTO3D_FACTORY__ || {};
  const params = new URLSearchParams(location.search);
  const captureMode = params.get('capture') === '1' || CONFIG.capture === true;
  const state = {
    spin: params.get('spin') === '1',
    wireframe: false,
    interactive: false,
    azimuth: Number(params.get('az') ?? (CONFIG.hero && CONFIG.hero.azimuth) ?? 35),
    elevation: Number(params.get('el') ?? (CONFIG.hero && CONFIG.hero.elevation) ?? 15),
    zoom: 1,
    focus: 'all',
    errors: [],
  };

  function fail(message) {
    state.errors.push(message);
    console.error('[auto3d] ' + message);
    const banner = document.createElement('pre');
    banner.style.cssText = 'position:fixed;left:8px;top:8px;max-width:90vw;white-space:pre-wrap;color:#fff;background:#b3261e;padding:8px 12px;border-radius:6px;font:12px/1.4 monospace;z-index:20';
    banner.textContent = message;
    document.body.appendChild(banner);
    window.__IMG2THREEJS_ERROR__ = message;
  }

  if (!THREE) {
    fail('three.js bundle did not load');
    return;
  }

  // ------------------------------------------------------------------ renderer
  const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true, alpha: false });
  renderer.setPixelRatio(captureMode ? 1 : Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(window.innerWidth, window.innerHeight);
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  document.body.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(CONFIG.background || '#f2f2f2');

  // ------------------------------------------------------------------ model
  let model = null;
  try {
    const create = FACTORY[CONFIG.factoryFn];
    if (typeof create !== 'function') throw new Error('factory export not found: ' + CONFIG.factoryFn);
    model = create({ qualityPriority: 'reference-fidelity' });
    if (!model || !model.isObject3D) throw new Error('factory did not return a THREE.Object3D');
    scene.add(model);
  } catch (error) {
    fail('factory failed: ' + (error && error.stack ? error.stack : error));
    window.__IMG2THREEJS_READY__ = true; // ready so the capture worker can read the error instead of timing out
    return;
  }

  const configure = FACTORY[CONFIG.configureFn];
  if (typeof configure === 'function') {
    try { configure(renderer); } catch (error) { console.warn('[auto3d] configureRenderer failed', error); }
  }
  const makeLights = FACTORY[CONFIG.lightsFn];
  let lights = null;
  if (typeof makeLights === 'function') {
    try { lights = makeLights('neutral'); } catch (error) { console.warn('[auto3d] lookdev lights failed', error); }
  }
  if (!lights) {
    lights = new THREE.Group();
    lights.add(new THREE.HemisphereLight(0xf2f4ff, 0x4a4a4a, 0.9));
    const key = new THREE.DirectionalLight(0xfff4e8, 2.2);
    key.position.set(-4, 6, 5.5);
    key.castShadow = true;
    key.shadow.mapSize.set(2048, 2048);
    lights.add(key);
    const fill = new THREE.DirectionalLight(0xa8c4ff, 0.45);
    fill.position.set(4, 3, 3.5);
    lights.add(fill);
    const rim = new THREE.DirectionalLight(0xfff1c4, 0.8);
    rim.position.set(0.5, 4.5, -6);
    lights.add(rim);
  }
  scene.add(lights);
  const makeEnvironment = FACTORY[CONFIG.envFn];
  if (typeof makeEnvironment === 'function') {
    try { scene.environment = makeEnvironment(renderer); } catch (error) { console.warn('[auto3d] environment failed', error); }
  }

  // ------------------------------------------------------------------ bounds
  const bounds = new THREE.Box3();
  const sphere = new THREE.Sphere();
  function refreshBounds() {
    model.updateMatrixWorld(true);
    bounds.setFromObject(model);
    if (bounds.isEmpty()) {
      bounds.set(new THREE.Vector3(-0.5, -0.5, -0.5), new THREE.Vector3(0.5, 0.5, 0.5));
    }
    bounds.getBoundingSphere(sphere);
    if (!(sphere.radius > 1e-6)) sphere.radius = 0.5;
  }
  refreshBounds();
  // A model that sits above/below the ground plane is fine; we only ever frame the bounds. Also
  // drop a shadow catcher just below the lowest point so contact shadows read like the reference.
  const catcher = new THREE.Mesh(new THREE.PlaneGeometry(sphere.radius * 40, sphere.radius * 40), new THREE.ShadowMaterial({ opacity: 0.18 }));
  catcher.rotation.x = -Math.PI / 2;
  catcher.position.y = bounds.min.y - sphere.radius * 0.002;
  catcher.receiveShadow = true;
  catcher.name = '__auto3d_shadow_catcher';
  // Interactive only: a contact shadow helps a human read where the model sits, but it would
  // leak into the silhouette masks the deterministic gates measure, so captures stay clean.
  if (CONFIG.groundShadow !== false && !captureMode) scene.add(catcher);

  // ------------------------------------------------------------------ camera
  const camera = new THREE.PerspectiveCamera(CONFIG.fovDegrees || 35, window.innerWidth / window.innerHeight, 0.01, 100);
  const focusTarget = new THREE.Vector3();

  function frame(spec) {
    refreshBounds();
    const az = THREE.MathUtils.degToRad(Number(spec.azimuthDegrees ?? spec.azimuth ?? state.azimuth));
    const el = THREE.MathUtils.degToRad(Number(spec.elevationDegrees ?? spec.elevation ?? state.elevation));
    const fov = Number(spec.fovDegrees || CONFIG.fovDegrees || 35);
    camera.fov = fov;
    const margin = Number(spec.margin || CONFIG.margin || 1.12);
    const role = spec.role || spec.focus || 'all';
    let radius = sphere.radius;
    let center = sphere.center.clone();
    if (role === 'head-closeup' || role === 'top') {
      // Frame the upper part of the subject: for characters this is the head, for objects the lid/top.
      const size = bounds.getSize(new THREE.Vector3());
      center = new THREE.Vector3(sphere.center.x, bounds.max.y - size.y * 0.14, sphere.center.z);
      radius = Math.max(size.y * 0.16, Math.max(size.x, size.z) * 0.35, 1e-3);
    }
    if (Array.isArray(spec.target) && spec.target.length === 3 && spec.targetSpace === 'world') {
      center = new THREE.Vector3(spec.target[0], spec.target[1], spec.target[2]);
    }
    const zoom = Number(spec.zoom || state.zoom || 1);
    let distance = (radius * margin) / Math.sin(THREE.MathUtils.degToRad(fov) / 2) / zoom;
    if (spec.distance) distance = Number(spec.distance);
    const dir = new THREE.Vector3(Math.sin(az) * Math.cos(el), Math.sin(el), Math.cos(az) * Math.cos(el));
    camera.position.copy(center).addScaledVector(dir, distance);
    camera.up.set(0, 1, 0);
    camera.near = spec.near ? Number(spec.near) : Math.max(0.005, distance - radius * 4);
    camera.far = spec.far ? Number(spec.far) : distance + radius * 8;
    camera.lookAt(center);
    camera.updateProjectionMatrix();
    focusTarget.copy(center);
    if (controls) {
      controls.target.copy(center);
      controls.update();
    }
    state.azimuth = THREE.MathUtils.radToDeg(az);
    state.elevation = THREE.MathUtils.radToDeg(el);
    return { azimuth: state.azimuth, elevation: state.elevation, distance, fov, center: center.toArray(), radius };
  }

  // ------------------------------------------------------------------ controls (interactive only)
  let controls = null;
  if (!captureMode) {
    try {
      const makeControls = FACTORY[CONFIG.controlsFn];
      const OrbitControls = window.__AUTO3D_ORBIT_CONTROLS__;
      if (typeof makeControls === 'function') controls = makeControls(camera, renderer.domElement);
      else if (OrbitControls) controls = new OrbitControls(camera, renderer.domElement);
      if (controls) {
        controls.enableDamping = true;
        controls.minDistance = 0.01;
        controls.maxDistance = 1000;
        controls.addEventListener('start', () => { state.interactive = true; });
      }
    } catch (error) {
      console.warn('[auto3d] controls unavailable', error);
    }
  }

  frame({ azimuthDegrees: state.azimuth, elevationDegrees: state.elevation });

  // ------------------------------------------------------------------ helpers
  function setWireframe(enabled) {
    state.wireframe = enabled;
    model.traverse((node) => {
      if (node.isMesh && node.material) {
        const materials = Array.isArray(node.material) ? node.material : [node.material];
        materials.forEach((material) => { material.wireframe = enabled; material.needsUpdate = true; });
      }
    });
  }

  function triangleCount() {
    let triangles = 0;
    model.traverse((node) => {
      if (node.isMesh && node.geometry) {
        const geometry = node.geometry;
        const index = geometry.getIndex();
        const position = geometry.getAttribute('position');
        if (!position) return;
        const count = index ? index.count : position.count;
        triangles += Math.floor(count / 3) * (node.isInstancedMesh ? node.count : 1);
      }
    });
    return triangles;
  }

  function waitFrames(n) {
    return new Promise((resolve) => {
      let remaining = Math.max(1, n | 0);
      function step() {
        remaining -= 1;
        if (remaining <= 0) resolve();
        else requestAnimationFrame(step);
      }
      requestAnimationFrame(step);
    });
  }

  function exportMeshes(options) {
    const maxTriangles = Number((options && options.maxTriangles) || 250000);
    const meshes = [];
    const skipped = [];
    const normalMatrix = new THREE.Matrix3();
    const v = new THREE.Vector3();
    model.updateMatrixWorld(true);
    model.traverse((node) => {
      if (!node.isMesh || !node.geometry) return;
      if (node.name === '__auto3d_shadow_catcher') return;
      const name = node.name || node.uuid;
      if (node.isInstancedMesh) { skipped.push({ name, reason: 'instanced mesh' }); return; }
      const geometry = node.geometry;
      const position = geometry.getAttribute('position');
      if (!position) { skipped.push({ name, reason: 'no position attribute' }); return; }
      const index = geometry.getIndex();
      const triangles = Math.floor((index ? index.count : position.count) / 3);
      if (triangles > maxTriangles) { skipped.push({ name, reason: 'over triangle cap ' + triangles }); return; }
      const vertices = new Array(position.count);
      for (let i = 0; i < position.count; i += 1) {
        v.fromBufferAttribute(position, i).applyMatrix4(node.matrixWorld);
        vertices[i] = [round(v.x), round(v.y), round(v.z)];
      }
      let normals = null;
      const normalAttr = geometry.getAttribute('normal');
      if (normalAttr && normalAttr.count === position.count) {
        normalMatrix.getNormalMatrix(node.matrixWorld);
        normals = new Array(position.count);
        for (let i = 0; i < position.count; i += 1) {
          v.fromBufferAttribute(normalAttr, i).applyMatrix3(normalMatrix).normalize();
          normals[i] = [round(v.x), round(v.y), round(v.z)];
        }
      }
      const indices = [];
      if (index) {
        for (let i = 0; i < index.count; i += 1) indices.push(index.getX(i));
      } else {
        for (let i = 0; i < position.count; i += 1) indices.push(i);
      }
      const record = { name, id: name, vertices, indices, triangles, realization: 'separate-geometry' };
      if (normals) record.normals = normals;
      if (node.userData && node.userData.sculptComponent) record.componentId = node.userData.sculptComponent.id;
      meshes.push(record);
    });
    return { meshes, skipped, space: 'world', generatedBy: 'codex_auto3d viewer' };
  }

  function round(value) { return Math.round(value * 1e6) / 1e6; }

  // Map-stripped evidence: the blockout Tier-1 diagnostic wants a render with every texture map
  // disabled so a convincing texture cannot stand in for real structure. Materials are cloned,
  // never mutated, so the beauty render is restored exactly.
  const MAP_SLOTS = ['map', 'roughnessMap', 'metalnessMap', 'normalMap', 'bumpMap', 'displacementMap', 'aoMap', 'emissiveMap', 'alphaMap', 'clearcoatMap', 'clearcoatNormalMap', 'clearcoatRoughnessMap', 'sheenColorMap', 'sheenRoughnessMap', 'specularIntensityMap', 'specularColorMap', 'transmissionMap', 'thicknessMap', 'iridescenceMap', 'anisotropyMap'];
  const originalMaterials = new Map();
  function stripMaps(enabled) {
    if (!enabled) {
      originalMaterials.forEach((material, mesh) => { mesh.material = material; });
      originalMaterials.clear();
      return;
    }
    model.traverse((node) => {
      if (!node.isMesh || !node.material || originalMaterials.has(node)) return;
      originalMaterials.set(node, node.material);
      const clone = (material) => {
        const copy = material.clone();
        MAP_SLOTS.forEach((slot) => { if (slot in copy) copy[slot] = null; });
        copy.needsUpdate = true;
        return copy;
      };
      node.material = Array.isArray(node.material) ? node.material.map(clone) : clone(node.material);
    });
  }

  // ------------------------------------------------------------------ contract
  window.__IMG2THREEJS_CAPTURE__ = {
    async setCamera(spec) {
      state.interactive = false;
      if (controls) controls.enabled = false;
      stripMaps(false);
      if (spec && spec.mapStripped) stripMaps(true);
      const applied = frame(spec || {});
      // Render explicitly instead of relying on the animation loop: in capture mode the loop is
      // idle so software GL (CI, SwiftShader) does not burn seconds per frame between shots.
      renderer.render(scene, camera);
      await waitFrames(1);
      renderer.render(scene, camera);
      await waitFrames(1);
      return Object.assign({ ok: true }, applied);
    },
    async capturePass(request) {
      const passId = request && request.passId;
      if (passId && passId !== 'beauty') {
        return { ok: false, reason: 'pass not supported by the auto3d viewer: ' + passId };
      }
      renderer.render(scene, camera);
      await waitFrames(1);
      return { ok: true, selector: 'canvas' };
    },
    setWireframe,
    getState() { return { azimuth: state.azimuth, elevation: state.elevation, triangles: triangleCount(), errors: state.errors }; },
  };
  window.__IMG2THREEJS_EXPORT_MESHES__ = exportMeshes;
  window.__AUTO3D_MODEL__ = model;

  // ------------------------------------------------------------------ UI (interactive only)
  if (!captureMode) {
    const panel = document.createElement('div');
    panel.id = 'auto3d-ui';
    const runtime = model.userData && model.userData.sculptRuntime;
    const nodeCount = runtime && runtime.nodes ? Object.keys(runtime.nodes).length : 0;
    panel.innerHTML =
      '<div class="title">' + (CONFIG.title || CONFIG.factoryFn || 'preview') + '</div>' +
      '<div class="meta">' + triangleCount().toLocaleString() + ' tris · ' + nodeCount + ' nodes · pass ' + (CONFIG.passId || '?') + '</div>' +
      '<div class="row">' +
      ['hero', 'front', 'side', 'back', 'top'].map((name) => '<button data-view="' + name + '">' + name + '</button>').join('') +
      '</div>' +
      '<div class="row"><button data-action="spin">turntable</button><button data-action="wire">wireframe</button><button data-action="bg">background</button></div>' +
      '<div class="hint">drag to orbit · wheel to zoom · keys 1-5 views, R turntable, W wireframe</div>';
    document.body.appendChild(panel);
    const views = CONFIG.views || { hero: { azimuth: 35, elevation: 15 }, front: { azimuth: 0, elevation: 0 }, side: { azimuth: 90, elevation: 0 }, back: { azimuth: 180, elevation: 0 }, top: { azimuth: 0, elevation: 80 } };
    panel.addEventListener('click', (event) => {
      const button = event.target.closest('button');
      if (!button) return;
      const view = button.dataset.view;
      if (view && views[view]) {
        state.interactive = false;
        if (controls) controls.enabled = true;
        frame({ azimuthDegrees: views[view].azimuth, elevationDegrees: views[view].elevation });
      }
      const action = button.dataset.action;
      if (action === 'spin') state.spin = !state.spin;
      if (action === 'wire') setWireframe(!state.wireframe);
      if (action === 'bg') {
        const dark = scene.background && scene.background.getHex && scene.background.getHex() !== 0x202124;
        scene.background = new THREE.Color(dark ? 0x202124 : (CONFIG.background || '#f2f2f2'));
      }
    });
    window.addEventListener('keydown', (event) => {
      const map = { '1': 'hero', '2': 'front', '3': 'side', '4': 'back', '5': 'top' };
      if (map[event.key] && views[map[event.key]]) {
        const view = views[map[event.key]];
        frame({ azimuthDegrees: view.azimuth, elevationDegrees: view.elevation });
      } else if (event.key === 'r' || event.key === 'R') state.spin = !state.spin;
      else if (event.key === 'w' || event.key === 'W') setWireframe(!state.wireframe);
    });
    if (controls) controls.enabled = true;
  }

  window.addEventListener('resize', () => {
    camera.aspect = window.innerWidth / window.innerHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(window.innerWidth, window.innerHeight);
  });

  // ------------------------------------------------------------------ loop
  if (captureMode) {
    // Deterministic: one warm-up render (compiles shaders, builds the PMREM), then idle until
    // setCamera() asks for frames. Nothing moves between shots.
    renderer.render(scene, camera);
    requestAnimationFrame(() => {
      renderer.render(scene, camera);
      window.__IMG2THREEJS_READY__ = true;
    });
  } else {
    let firstFrame = true;
    const loop = () => {
      requestAnimationFrame(loop);
      if (state.spin) model.rotation.y += 0.01;
      if (controls && state.interactive && controls.enabled) controls.update();
      if (model.userData && typeof model.userData.tick === 'function') {
        try { model.userData.tick(performance.now() / 1000); } catch (error) { /* ignore */ }
      }
      renderer.render(scene, camera);
      if (firstFrame) {
        firstFrame = false;
        window.__IMG2THREEJS_READY__ = true;
      }
    };
    loop();
  }
})();
