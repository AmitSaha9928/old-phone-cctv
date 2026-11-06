package com.oldphonecctv.camera;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.graphics.ImageFormat;
import android.graphics.Rect;
import android.graphics.YuvImage;
import android.media.Image;
import android.media.ImageReader;
import android.hardware.camera2.CameraCaptureSession;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraDevice;
import android.hardware.camera2.CameraManager;
import android.hardware.camera2.CaptureRequest;
import android.hardware.camera2.params.StreamConfigurationMap;
import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
import android.media.AudioTrack;
import android.media.MediaCodec;
import android.media.MediaCodecInfo;
import android.media.MediaFormat;
import android.media.MediaRecorder;
import android.net.wifi.WifiManager;
import android.os.BatteryManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.os.PowerManager;
import android.os.SystemClock;
import android.util.Log;
import android.util.Range;
import android.util.Size;
import android.view.Surface;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.IOException;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.NetworkInterface;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.Collections;
import java.util.Enumeration;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;

/**
 * The whole camera node: camera + mic capture, H.264 encoding, a single-viewer TCP link to the
 * PC, talk-back playback, and a LAN beacon so the PC can find the phone without configuration.
 *
 * Wire format, both directions: [type u8][length u32 big-endian][payload].
 */
public class CamService extends Service {
    static final String TAG = "OldPhone CCTV";
    static final int PORT = 47801;
    static final int BEACON_PORT = 47800;

    // phone -> PC
    static final int T_HELLO = 1, T_VIDEO = 2, T_AUDIO = 3, T_STATUS = 4, T_MOTION = 5, T_SNAPSHOT = 6;
    // PC -> phone
    static final int T_TALK = 0x10, T_CMD = 0x11;

    static final int AUDIO_RATE = 16000;
    static final int AUDIO_CHUNK = 1280;            // 40 ms of 16 kHz mono s16le
    static final long MAX_QUEUE_BYTES = 3_000_000;  // ~3 s of video at 8 Mbps; beyond that we drop to next keyframe

    static final String ACTION_STOP = "com.oldphonecctv.camera.STOP";

    // read by MainActivity for its status screen
    static volatile boolean running;
    static volatile String camState = "stopped";
    static volatile String viewer = null;
    static volatile String lastError = null;

    SharedPreferences prefs;
    PowerManager.WakeLock wakeLock;
    WifiManager.WifiLock wifiLock;
    HandlerThread camThread;
    Handler camHandler;

    // video
    CameraDevice camera;
    CameraCaptureSession session;
    MediaCodec encoder;
    Surface encSurface;
    Thread drainThread;
    volatile boolean videoOn;
    volatile byte[] codecConfig;
    volatile boolean tsRealtime = true;
    volatile String videoDesc = "";
    int retryDelayMs = 3000;

    // motion detection (runs on a small YUV copy of the camera stream)
    volatile ImageReader reader;
    volatile boolean motionSupported = true;
    HandlerThread motionThread;
    Handler motionHandler;
    final Motion motion = new Motion();

    // audio
    Thread audioThread;
    volatile boolean audioOn;

    // talk-back
    final LinkedBlockingQueue<byte[]> talkQ = new LinkedBlockingQueue<>();
    Thread talkThread;

    // network
    ServerSocket server;
    Thread acceptThread, beaconThread, statusThread;
    volatile Client client;

    // stats
    final AtomicLong frames = new AtomicLong(), videoBytes = new AtomicLong();

    // ------------------------------------------------------------------ lifecycle

    @Override
    public void onCreate() {
        super.onCreate();
        prefs = getSharedPreferences("cfg", MODE_PRIVATE);
        // A stable id lets the PC tell several phones apart. No key is set here: the phone pairs
        // with the first PC that connects (or the key the PC pushes over adb).
        if (prefs.getString("id", null) == null) {
            prefs.edit().putString("id", randomKey().toLowerCase()).apply();
        }
    }

    String camName() { return prefs.getString("name", Build.MODEL); }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            prefs.edit().putBoolean("enabled", false).apply();
            stopSelf();
            return START_NOT_STICKY;
        }
        prefs.edit().putBoolean("enabled", true).apply();
        startForeground(1, buildNotification("Starting camera"));
        if (!running) startAll();
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        stopAll();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) { return null; }

    void startAll() {
        running = true;
        lastError = null;
        PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "OldPhoneCCTV:stream");
        wakeLock.acquire();
        WifiManager wm = (WifiManager) getApplicationContext().getSystemService(WIFI_SERVICE);
        wifiLock = wm.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "OldPhoneCCTV:wifi");
        wifiLock.acquire();

        motionThread = new HandlerThread("motion");
        motionThread.start();
        motionHandler = new Handler(motionThread.getLooper());
        camThread = new HandlerThread("cam");
        camThread.start();
        camHandler = new Handler(camThread.getLooper());
        camHandler.post(this::startVideo);

        if (prefs.getBoolean("audio", true)) startAudio();

        talkThread = new Thread(this::talkLoop, "talk");
        talkThread.start();
        acceptThread = new Thread(this::acceptLoop, "accept");
        acceptThread.start();
        beaconThread = new Thread(this::beaconLoop, "beacon");
        beaconThread.start();
        statusThread = new Thread(this::statusLoop, "status");
        statusThread.start();
    }

    void stopAll() {
        running = false;
        camState = "stopped";
        try { if (server != null) server.close(); } catch (IOException ignored) {}
        Client c = client;
        if (c != null) c.close();
        stopAudio();
        if (camHandler != null) {
            camHandler.post(this::stopVideo);
            camThread.quitSafely();
            try { camThread.join(3000); } catch (InterruptedException ignored) {}
        }
        if (motionThread != null) motionThread.quitSafely();
        if (talkThread != null) talkThread.interrupt();
        if (beaconThread != null) beaconThread.interrupt();
        if (statusThread != null) statusThread.interrupt();
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        if (wifiLock != null && wifiLock.isHeld()) wifiLock.release();
    }

    // ------------------------------------------------------------------ video

    void startVideo() {
        stopVideo();
        if (!running) return;
        camState = "opening camera";
        try {
            CameraManager cm = (CameraManager) getSystemService(CAMERA_SERVICE);
            String id = pickCamera(cm, prefs.getString("camera", "back"));
            if (id == null) throw new IllegalStateException("no camera found (camera service reports 0 cameras)");
            CameraCharacteristics cc = cm.getCameraCharacteristics(id);
            StreamConfigurationMap map = cc.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
            Size size = pickSize(map.getOutputSizes(MediaCodec.class),
                    prefs.getInt("width", 1280), prefs.getInt("height", 720));
            int fps = prefs.getInt("fps", 15);
            Range<Integer> fpsRange = pickFps(cc.get(CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES), fps);
            Integer tsSource = cc.get(CameraCharacteristics.SENSOR_INFO_TIMESTAMP_SOURCE);
            tsRealtime = tsSource != null && tsSource == CameraCharacteristics.SENSOR_INFO_TIMESTAMP_SOURCE_REALTIME;

            encoder = MediaCodec.createEncoderByType("video/avc");
            int bitrate = prefs.getInt("bitrate", 1_000_000);
            boolean configured = false;
            for (int attempt = 0; attempt < 2 && !configured; attempt++) {
                MediaFormat f = MediaFormat.createVideoFormat("video/avc", size.getWidth(), size.getHeight());
                f.setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface);
                f.setInteger(MediaFormat.KEY_BIT_RATE, bitrate);
                f.setInteger(MediaFormat.KEY_FRAME_RATE, fps);
                f.setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 2);
                f.setInteger(MediaFormat.KEY_BITRATE_MODE, MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_VBR);
                if (attempt == 0) {
                    // High profile compresses noticeably better; fall back to the default if refused.
                    f.setInteger(MediaFormat.KEY_PROFILE, MediaCodecInfo.CodecProfileLevel.AVCProfileHigh);
                    f.setInteger(MediaFormat.KEY_LEVEL, MediaCodecInfo.CodecProfileLevel.AVCLevel4);
                }
                try {
                    encoder.configure(f, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE);
                    configured = true;
                } catch (Exception e) {
                    encoder.reset();
                }
            }
            if (!configured) throw new IllegalStateException("video encoder refused " + size);
            encSurface = encoder.createInputSurface();
            encoder.start();
            videoOn = true;
            final MediaCodec enc = encoder;
            drainThread = new Thread(() -> drainLoop(enc), "drain");
            drainThread.start();
            videoDesc = size.getWidth() + "x" + size.getHeight() + " @" + fpsRange;

            // Small second stream for motion detection (skipped if disabled or unsupported).
            motion.reset();
            if (prefs.getBoolean("motion", true) && motionSupported) {
                Size ms = pickMotionSize(map.getOutputSizes(ImageFormat.YUV_420_888), size);
                if (ms != null) {
                    reader = ImageReader.newInstance(ms.getWidth(), ms.getHeight(), ImageFormat.YUV_420_888, 2);
                    reader.setOnImageAvailableListener(this::onMotionFrame, motionHandler);
                }
            }

            cm.openCamera(id, new CameraDevice.StateCallback() {
                @Override public void onOpened(CameraDevice d) {
                    camera = d;
                    createSession(d, fpsRange);
                }
                @Override public void onDisconnected(CameraDevice d) { fail("camera disconnected", null); }
                @Override public void onError(CameraDevice d, int error) { fail("camera error " + error, null); }
            }, camHandler);
        } catch (Exception e) {
            fail("could not start camera", e);
        }
    }

    void createSession(CameraDevice d, Range<Integer> fpsRange) {
        final ImageReader r = reader;
        java.util.List<Surface> outs = new java.util.ArrayList<>();
        outs.add(encSurface);
        if (r != null) outs.add(r.getSurface());
        try {
            d.createCaptureSession(outs, new CameraCaptureSession.StateCallback() {
                @Override public void onConfigured(CameraCaptureSession s) {
                    session = s;
                    try {
                        applyRequest(fpsRange);
                        camState = "streaming";
                        retryDelayMs = 3000;
                        lastError = null;
                        sendHello();
                        updateNotification();
                    } catch (Exception e) { fail("capture request failed", e); }
                }
                @Override public void onConfigureFailed(CameraCaptureSession s) {
                    if (r != null) {
                        // Some camera drivers can't do video + a second stream: run without motion.
                        Log.w(TAG, "camera refused the motion stream; motion detection off");
                        motionSupported = false;
                        reader = null;
                        r.close();
                        createSession(d, fpsRange);
                    } else {
                        fail("camera refused the stream configuration", null);
                    }
                }
            }, camHandler);
        } catch (Exception e) { fail("could not create capture session", e); }
    }

    Range<Integer> currentFps;

    void applyRequest(Range<Integer> fpsRange) throws Exception {
        if (fpsRange != null) currentFps = fpsRange;
        if (camera == null || session == null) return;
        CaptureRequest.Builder b = camera.createCaptureRequest(CameraDevice.TEMPLATE_RECORD);
        b.addTarget(encSurface);
        if (reader != null) b.addTarget(reader.getSurface());
        b.set(CaptureRequest.CONTROL_MODE, CaptureRequest.CONTROL_MODE_AUTO);
        b.set(CaptureRequest.CONTROL_AE_MODE, CaptureRequest.CONTROL_AE_MODE_ON);
        b.set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_VIDEO);
        b.set(CaptureRequest.CONTROL_VIDEO_STABILIZATION_MODE, CaptureRequest.CONTROL_VIDEO_STABILIZATION_MODE_OFF);
        if (currentFps != null) b.set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, currentFps);
        b.set(CaptureRequest.FLASH_MODE, prefs.getBoolean("torch", false)
                ? CaptureRequest.FLASH_MODE_TORCH : CaptureRequest.FLASH_MODE_OFF);
        session.setRepeatingRequest(b.build(), null, camHandler);
    }

    void fail(String what, Exception e) {
        String msg = what + (e != null ? ": " + e.getMessage() : "");
        Log.e(TAG, msg, e);
        lastError = msg;
        camState = "error";
        updateNotification();
        if (camHandler != null && running) {
            camHandler.post(this::stopVideo);
            camHandler.postDelayed(this::startVideo, retryDelayMs);
            retryDelayMs = Math.min(retryDelayMs * 2, 60_000);
        }
    }

    void stopVideo() {
        videoOn = false;
        try { if (session != null) session.close(); } catch (Exception ignored) {}
        session = null;
        try { if (camera != null) camera.close(); } catch (Exception ignored) {}
        camera = null;
        if (reader != null) { try { reader.close(); } catch (Exception ignored) {} reader = null; }
        if (motion.active) endMotion();
        if (drainThread != null) {
            try { drainThread.join(1000); } catch (InterruptedException ignored) {}
            drainThread = null;
        }
        try { if (encoder != null) { encoder.stop(); } } catch (Exception ignored) {}
        try { if (encoder != null) { encoder.release(); } } catch (Exception ignored) {}
        encoder = null;
        if (encSurface != null) { encSurface.release(); encSurface = null; }
        codecConfig = null;
    }

    void drainLoop(MediaCodec enc) {
        MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
        while (videoOn) {
            int idx;
            try {
                idx = enc.dequeueOutputBuffer(info, 200_000);
            } catch (Exception e) { return; }
            if (idx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                MediaFormat of = enc.getOutputFormat();
                ByteBuffer sps = of.getByteBuffer("csd-0"), pps = of.getByteBuffer("csd-1");
                if (sps != null && pps != null) {
                    byte[] cfg = new byte[sps.remaining() + pps.remaining()];
                    sps.get(cfg, 0, sps.remaining());
                    pps.get(cfg, cfg.length - pps.remaining(), pps.remaining());
                    codecConfig = cfg;
                    sendVideo(2, 0, cfg);
                }
                continue;
            }
            if (idx < 0) continue;
            ByteBuffer buf = enc.getOutputBuffer(idx);
            byte[] data = new byte[info.size];
            if (buf != null) {
                buf.position(info.offset);
                buf.get(data, 0, info.size);
            }
            int flags = info.flags;
            long pts = info.presentationTimeUs;
            try { enc.releaseOutputBuffer(idx, false); } catch (Exception e) { return; }
            if ((flags & MediaCodec.BUFFER_FLAG_CODEC_CONFIG) != 0) {
                codecConfig = data;
                sendVideo(2, 0, data);
                continue;
            }
            if (data.length == 0) continue;
            frames.incrementAndGet();
            videoBytes.addAndGet(data.length);
            sendVideo((flags & MediaCodec.BUFFER_FLAG_KEY_FRAME) != 0 ? 1 : 0, pts, data);
        }
    }

    void requestKeyFrame() {
        MediaCodec enc = encoder;
        if (enc == null) return;
        try {
            Bundle b = new Bundle();
            b.putInt(MediaCodec.PARAMETER_KEY_REQUEST_SYNC_FRAME, 0);
            enc.setParameters(b);
        } catch (Exception ignored) {}
    }

    void sendVideo(int flags, long pts, byte[] data) {
        Client c = client;
        if (c == null || !c.authed) return;
        boolean key = (flags & 1) != 0, config = (flags & 2) != 0;
        if (!config && !key) {
            if (c.dropUntilKey) return;
            if (c.queued.get() > MAX_QUEUE_BYTES) {
                c.dropUntilKey = true;
                requestKeyFrame();
                return;
            }
        }
        if (key) c.dropUntilKey = false;
        byte[] p = new byte[9 + data.length];
        p[0] = (byte) flags;
        putLong(p, 1, pts);
        System.arraycopy(data, 0, p, 9, data.length);
        c.send(T_VIDEO, p);
    }

    /** Camera ids apps may open. (The Mi A2's extra rear sensors report details but refuse to open
     *  for anything but Xiaomi's own camera app, so they are left out.) */
    static java.util.List<String> allCameraIds(CameraManager cm) {
        java.util.List<String> out = new java.util.ArrayList<>();
        try { out.addAll(java.util.Arrays.asList(cm.getCameraIdList())); } catch (Exception ignored) {}
        return out;
    }

    static String pickCamera(CameraManager cm, String want) throws Exception {
        java.util.List<String> all = allCameraIds(cm);
        if (all.isEmpty()) return null;
        if (all.contains(want)) return want;
        String[] ids = all.toArray(new String[0]);
        int facing = "front".equals(want) ? CameraCharacteristics.LENS_FACING_FRONT : CameraCharacteristics.LENS_FACING_BACK;
        for (String id : ids) {
            Integer f = cm.getCameraCharacteristics(id).get(CameraCharacteristics.LENS_FACING);
            if (f != null && f == facing) return id;
        }
        return ids[0];
    }

    static Size pickSize(Size[] sizes, int w, int h) {
        Size best = null;
        long want = (long) w * h, bestScore = Long.MAX_VALUE;
        for (Size s : sizes) {
            if (s.getWidth() == w && s.getHeight() == h) return s;
            if (s.getWidth() > 1920) continue;
            boolean sameAspect = Math.abs((double) s.getWidth() / s.getHeight() - (double) w / h) < 0.02;
            long score = Math.abs((long) s.getWidth() * s.getHeight() - want) + (sameAspect ? 0 : 10_000_000L);
            if (score < bestScore) { bestScore = score; best = s; }
        }
        return best != null ? best : sizes[0];
    }

    /** Smallest YUV size of at least 320 px wide with the video's aspect ratio (cheap to analyse). */
    static Size pickMotionSize(Size[] sizes, Size video) {
        if (sizes == null) return null;
        double want = (double) video.getWidth() / video.getHeight();
        Size best = null;
        for (Size s : sizes) {
            if (s.getWidth() < 320 || s.getWidth() > 800) continue;
            if (Math.abs((double) s.getWidth() / s.getHeight() - want) > 0.05) continue;
            if (best == null || s.getWidth() < best.getWidth()) best = s;
        }
        if (best == null) {
            for (Size s : sizes) if (s.getWidth() >= 320 && s.getWidth() <= 800 && (best == null || s.getWidth() < best.getWidth())) best = s;
        }
        return best;
    }

    // ------------------------------------------------------------------ motion detection

    /** Compares a 32x18 grid of brightness cells against a slowly adapting background. */
    static class Motion {
        static final int GW = 32, GH = 18;
        float[] bg;
        long lastProcess, lastMotion, startedAt;
        int hits;
        boolean active;
        float peak, score;
        void reset() { bg = null; hits = 0; active = false; peak = 0; score = 0; }
    }

    void onMotionFrame(ImageReader r) {
        Image img;
        try { img = r.acquireLatestImage(); } catch (Exception e) { return; }
        if (img == null) return;
        try {
            long now = SystemClock.elapsedRealtime();
            if (now - motion.lastProcess < 300) return;   // ~3 checks a second is plenty
            motion.lastProcess = now;
            Image.Plane y = img.getPlanes()[0];
            ByteBuffer buf = y.getBuffer();
            int rs = y.getRowStride(), ps = y.getPixelStride(), w = img.getWidth(), h = img.getHeight();
            float[] cells = new float[Motion.GW * Motion.GH];
            for (int gy = 0; gy < Motion.GH; gy++) {
                for (int gx = 0; gx < Motion.GW; gx++) {
                    int sum = 0;
                    for (int sy = 0; sy < 2; sy++) for (int sx = 0; sx < 2; sx++) {
                        int px = (gx * 2 + sx) * w / (Motion.GW * 2) + w / (Motion.GW * 4);
                        int py = (gy * 2 + sy) * h / (Motion.GH * 2) + h / (Motion.GH * 4);
                        sum += buf.get(py * rs + px * ps) & 0xFF;
                    }
                    cells[gy * Motion.GW + gx] = sum / 4f;
                }
            }
            if (motion.bg == null) { motion.bg = cells; return; }
            // Ignore whole-picture brightness shifts (lights, clouds, auto-exposure).
            float shift = 0;
            for (int i = 0; i < cells.length; i++) shift += cells[i] - motion.bg[i];
            shift /= cells.length;
            int sens = prefs.getInt("sensitivity", 50);           // 0..100
            float thr = 30 - sens * 0.22f;                         // per-cell change needed
            int changed = 0;
            for (int i = 0; i < cells.length; i++) if (Math.abs(cells[i] - motion.bg[i] - shift) > thr) changed++;
            float frac = changed / (float) cells.length;
            float minFrac = 0.012f + (100 - sens) * 0.0006f;       // share of the picture that must change
            motion.score = frac;
            float adapt = motion.active ? 0.02f : 0.08f;
            for (int i = 0; i < cells.length; i++) motion.bg[i] += (cells[i] - motion.bg[i]) * adapt;

            if (frac > minFrac) {
                motion.hits++;
                motion.lastMotion = now;
                if (motion.active) motion.peak = Math.max(motion.peak, frac);
                else if (motion.hits >= 2) startMotion(img, frac);
            } else {
                motion.hits = 0;
                if (motion.active && now - motion.lastMotion > 5000) endMotion();
            }
        } catch (Exception e) {
            Log.w(TAG, "motion frame", e);
        } finally {
            img.close();
        }
    }

    void startMotion(Image img, float frac) {
        motion.active = true;
        motion.peak = frac;
        motion.startedAt = System.currentTimeMillis();
        Client c = client;
        if (c == null || !c.authed) return;
        try {
            JSONObject j = new JSONObject();
            j.put("on", true);
            j.put("score", Math.round(frac * 1000) / 1000.0);
            j.put("t", motion.startedAt);
            c.send(T_MOTION, j.toString().getBytes(StandardCharsets.UTF_8));
            byte[] jpg = toJpeg(img);
            if (jpg != null) c.send(T_SNAPSHOT, jpg);
        } catch (Exception ignored) {}
    }

    void endMotion() {
        motion.active = false;
        Client c = client;
        if (c == null || !c.authed) return;
        try {
            JSONObject j = new JSONObject();
            j.put("on", false);
            j.put("peak", Math.round(motion.peak * 1000) / 1000.0);
            j.put("t", System.currentTimeMillis());
            c.send(T_MOTION, j.toString().getBytes(StandardCharsets.UTF_8));
        } catch (Exception ignored) {}
    }

    /** YUV_420_888 -> NV21 -> JPEG, for the motion snapshot. */
    static byte[] toJpeg(Image img) {
        try {
            int w = img.getWidth(), h = img.getHeight();
            byte[] nv21 = new byte[w * h * 3 / 2];
            Image.Plane[] p = img.getPlanes();
            ByteBuffer yb = p[0].getBuffer(), ub = p[1].getBuffer(), vb = p[2].getBuffer();
            int yrs = p[0].getRowStride(), yps = p[0].getPixelStride();
            int i = 0;
            for (int row = 0; row < h; row++) for (int col = 0; col < w; col++) nv21[i++] = yb.get(row * yrs + col * yps);
            int crs = p[1].getRowStride(), cps = p[1].getPixelStride();
            for (int row = 0; row < h / 2; row++) {
                for (int col = 0; col < w / 2; col++) {
                    int off = row * crs + col * cps;
                    nv21[i++] = vb.get(off);
                    nv21[i++] = ub.get(off);
                }
            }
            java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
            new YuvImage(nv21, ImageFormat.NV21, w, h, null).compressToJpeg(new Rect(0, 0, w, h), 75, out);
            return out.toByteArray();
        } catch (Exception e) {
            return null;
        }
    }

    static Range<Integer> pickFps(Range<Integer>[] ranges, int fps) {
        if (ranges == null || ranges.length == 0) return null;
        Range<Integer> best = null;
        // Prefer a range topping out at our fps with the lowest floor: in the dark the
        // camera can then lengthen exposure instead of producing a black, noisy picture.
        for (Range<Integer> r : ranges) {
            if (r.getUpper() == fps && (best == null || r.getLower() < best.getLower())) best = r;
        }
        if (best != null) return best;
        for (Range<Integer> r : ranges) {
            if (r.getUpper() >= fps && (best == null || r.getUpper() < best.getUpper()
                    || (r.getUpper().equals(best.getUpper()) && r.getLower() < best.getLower()))) best = r;
        }
        return best != null ? best : ranges[0];
    }

    // ------------------------------------------------------------------ audio capture

    void startAudio() {
        if (audioOn) return;
        audioOn = true;
        audioThread = new Thread(this::audioLoop, "mic");
        audioThread.start();
    }

    void stopAudio() {
        audioOn = false;
        if (audioThread != null) {
            try { audioThread.join(1000); } catch (InterruptedException ignored) {}
            audioThread = null;
        }
    }

    long nowUs() {
        return tsRealtime ? SystemClock.elapsedRealtimeNanos() / 1000 : System.nanoTime() / 1000;
    }

    void audioLoop() {
        int min = AudioRecord.getMinBufferSize(AUDIO_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
        int bufSize = Math.max(min, AUDIO_RATE); // 0.5 s
        AudioRecord rec = null;
        for (int src : new int[]{MediaRecorder.AudioSource.CAMCORDER, MediaRecorder.AudioSource.MIC}) {
            try {
                rec = new AudioRecord(src, AUDIO_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, bufSize);
                if (rec.getState() == AudioRecord.STATE_INITIALIZED) break;
                rec.release();
                rec = null;
            } catch (Exception e) { rec = null; }
        }
        if (rec == null) { lastError = "microphone unavailable"; audioOn = false; return; }
        try {
            rec.startRecording();
            byte[] buf = new byte[AUDIO_CHUNK];
            while (audioOn && running) {
                int got = 0;
                while (got < buf.length && audioOn) {
                    int n = rec.read(buf, got, buf.length - got);
                    if (n <= 0) break;
                    got += n;
                }
                if (got < buf.length) { SystemClock.sleep(20); continue; }
                Client c = client;
                if (c == null || !c.authed) continue;
                long pts = nowUs() - 40_000;
                byte[] p = new byte[8 + got];
                putLong(p, 0, pts);
                System.arraycopy(buf, 0, p, 8, got);
                c.send(T_AUDIO, p);
            }
        } catch (Exception e) {
            lastError = "microphone: " + e.getMessage();
        } finally {
            try { rec.stop(); } catch (Exception ignored) {}
            rec.release();
        }
    }

    // ------------------------------------------------------------------ talk-back playback

    void talkLoop() {
        AudioTrack track = null;
        long last = 0;
        while (running) {
            byte[] d;
            try { d = talkQ.poll(500, TimeUnit.MILLISECONDS); } catch (InterruptedException e) { break; }
            long now = SystemClock.elapsedRealtime();
            if (d != null) {
                if (track == null) track = openTrack();
                if (track != null) track.write(d, 0, d.length);
                last = now;
            } else if (track != null && now - last > 3000) {
                try { track.stop(); } catch (Exception ignored) {}
                track.release();
                track = null;
            }
        }
        if (track != null) track.release();
    }

    AudioTrack openTrack() {
        try {
            AudioManager am = (AudioManager) getSystemService(AUDIO_SERVICE);
            int max = am.getStreamMaxVolume(AudioManager.STREAM_MUSIC);
            int vol = Math.round(max * prefs.getInt("volume", 90) / 100f);
            am.setStreamVolume(AudioManager.STREAM_MUSIC, vol, 0);
            int min = AudioTrack.getMinBufferSize(AUDIO_RATE, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT);
            AudioTrack t = new AudioTrack.Builder()
                    .setAudioAttributes(new AudioAttributes.Builder()
                            .setUsage(AudioAttributes.USAGE_MEDIA)
                            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                    .setAudioFormat(new AudioFormat.Builder()
                            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                            .setSampleRate(AUDIO_RATE)
                            .setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
                    .setBufferSizeInBytes(Math.max(min, AUDIO_RATE)) // 0.5 s
                    .setTransferMode(AudioTrack.MODE_STREAM)
                    .build();
            t.play();
            return t;
        } catch (Exception e) {
            lastError = "speaker: " + e.getMessage();
            return null;
        }
    }

    // ------------------------------------------------------------------ network

    void acceptLoop() {
        try {
            server = new ServerSocket();
            server.setReuseAddress(true);
            server.bind(new InetSocketAddress(PORT));
            while (running) {
                Socket s = server.accept();
                new Client(s).start();   // becomes the active client only after a correct key
            }
        } catch (IOException e) {
            if (running) lastError = "network: " + e.getMessage();
        }
    }

    void beaconLoop() {
        try (DatagramSocket ds = new DatagramSocket()) {
            ds.setBroadcast(true);
            while (running) {
                JSONObject j = new JSONObject();
                j.put("oldphonecctv", 1);
                j.put("id", prefs.getString("id", ""));
                j.put("name", camName());
                j.put("paired", prefs.getString("key", null) != null);
                j.put("port", PORT);
                byte[] b = j.toString().getBytes(StandardCharsets.UTF_8);
                try {
                    ds.send(new DatagramPacket(b, b.length, InetAddress.getByName("255.255.255.255"), BEACON_PORT));
                } catch (IOException ignored) {}
                Thread.sleep(client != null ? 15000 : 3000);
            }
        } catch (Exception ignored) {}
    }

    void statusLoop() {
        long lastFrames = 0, lastBytes = 0, lastT = SystemClock.elapsedRealtime();
        while (running) {
            try { Thread.sleep(2000); } catch (InterruptedException e) { break; }
            long t = SystemClock.elapsedRealtime();
            long f = frames.get(), b = videoBytes.get();
            double dt = Math.max(1, t - lastT) / 1000.0;
            double fps = (f - lastFrames) / dt, kbps = (b - lastBytes) * 8 / dt / 1000;
            lastFrames = f; lastBytes = b; lastT = t;
            Client c = client;
            if (c == null || !c.authed) continue;
            try {
                JSONObject j = new JSONObject();
                Intent bat = registerReceiver(null, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
                if (bat != null) {
                    int level = bat.getIntExtra(BatteryManager.EXTRA_LEVEL, -1);
                    int scale = bat.getIntExtra(BatteryManager.EXTRA_SCALE, 100);
                    j.put("battery", Math.round(level * 100f / scale));
                    j.put("charging", bat.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) != 0);
                    j.put("temp", bat.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, 0) / 10.0);
                }
                j.put("fps", Math.round(fps * 10) / 10.0);
                j.put("kbps", Math.round(kbps));
                j.put("cam", camState);
                j.put("video", videoDesc);
                j.put("audio", audioOn);
                j.put("motion", motion.active);
                j.put("motionScore", Math.round(motion.score * 1000) / 1000.0);
                j.put("error", lastError == null ? JSONObject.NULL : lastError);
                WifiManager wm = (WifiManager) getApplicationContext().getSystemService(WIFI_SERVICE);
                j.put("rssi", wm.getConnectionInfo().getRssi());
                j.put("uptime", SystemClock.elapsedRealtime() / 1000);
                c.send(T_STATUS, j.toString().getBytes(StandardCharsets.UTF_8));
            } catch (Exception ignored) {}
        }
    }

    void sendHello() {
        Client c = client;
        if (c == null || !c.authed) return;
        try {
            JSONObject j = new JSONObject();
            j.put("id", prefs.getString("id", ""));
            j.put("name", camName());
            j.put("model", Build.MANUFACTURER + " " + Build.MODEL);
            j.put("android", Build.VERSION.RELEASE);
            j.put("app", 2);
            j.put("motionAvailable", motionSupported && reader != null);
            JSONObject cfg = new JSONObject();
            cfg.put("camera", prefs.getString("camera", "back"));
            cfg.put("width", prefs.getInt("width", 1280));
            cfg.put("height", prefs.getInt("height", 720));
            cfg.put("fps", prefs.getInt("fps", 15));
            cfg.put("bitrate", prefs.getInt("bitrate", 1_000_000));
            cfg.put("audio", prefs.getBoolean("audio", true));
            cfg.put("torch", prefs.getBoolean("torch", false));
            cfg.put("volume", prefs.getInt("volume", 90));
            cfg.put("motion", prefs.getBoolean("motion", true));
            cfg.put("sensitivity", prefs.getInt("sensitivity", 50));
            j.put("cfg", cfg);
            j.put("video", videoDesc);
            j.put("audioRate", AUDIO_RATE);
            JSONArray cams = new JSONArray();
            try {
                CameraManager cm = (CameraManager) getSystemService(CAMERA_SERVICE);
                java.util.List<String> listed = java.util.Arrays.asList(cm.getCameraIdList());
                for (String id : allCameraIds(cm)) {
                    CameraCharacteristics ch = cm.getCameraCharacteristics(id);
                    Integer f = ch.get(CameraCharacteristics.LENS_FACING);
                    JSONObject o = new JSONObject();
                    o.put("id", id);
                    o.put("facing", f == null ? "?" : f == CameraCharacteristics.LENS_FACING_FRONT ? "front" : f == CameraCharacteristics.LENS_FACING_BACK ? "back" : "external");
                    android.util.Size px = ch.get(CameraCharacteristics.SENSOR_INFO_PIXEL_ARRAY_SIZE);
                    if (px != null) o.put("mp", Math.round(px.getWidth() * (double) px.getHeight() / 1e5) / 10.0);
                    float[] fl = ch.get(CameraCharacteristics.LENS_INFO_AVAILABLE_FOCAL_LENGTHS);
                    if (fl != null && fl.length > 0) o.put("focal", fl[0]);
                    o.put("hidden", !listed.contains(id));
                    cams.put(o);
                }
            } catch (Exception ignored) {}
            j.put("cameras", cams);
            c.send(T_HELLO, j.toString().getBytes(StandardCharsets.UTF_8));
            byte[] cfgBytes = codecConfig;
            if (cfgBytes != null) sendVideo(2, 0, cfgBytes);
            requestKeyFrame();
        } catch (Exception ignored) {}
    }

    void handleCommand(JSONObject j) throws Exception {
        String cmd = j.optString("cmd");
        switch (cmd) {
            case "key":
                requestKeyFrame();
                break;
            case "ping":
                break;
            case "hello":
                sendHello();
                break;
            case "set": {
                SharedPreferences.Editor e = prefs.edit();
                boolean restartVideo = false;
                if (j.has("camera")) { e.putString("camera", j.getString("camera")); restartVideo = true; }
                if (j.has("width")) { e.putInt("width", j.getInt("width")); restartVideo = true; }
                if (j.has("height")) { e.putInt("height", j.getInt("height")); restartVideo = true; }
                if (j.has("fps")) { e.putInt("fps", j.getInt("fps")); restartVideo = true; }
                if (j.has("bitrate")) { e.putInt("bitrate", j.getInt("bitrate")); restartVideo = true; }
                if (j.has("volume")) e.putInt("volume", Math.max(0, Math.min(100, j.getInt("volume"))));
                if (j.has("torch")) e.putBoolean("torch", j.getBoolean("torch"));
                if (j.has("audio")) e.putBoolean("audio", j.getBoolean("audio"));
                if (j.has("name")) e.putString("name", j.getString("name").trim());
                if (j.has("sensitivity")) e.putInt("sensitivity", Math.max(0, Math.min(100, j.getInt("sensitivity"))));
                if (j.has("motion")) {
                    e.putBoolean("motion", j.getBoolean("motion"));
                    motionSupported = true;   // let it try the second stream again
                    restartVideo = true;
                }
                e.commit();
                if (j.has("sensitivity")) motion.reset();
                if (j.has("audio")) { if (j.getBoolean("audio")) startAudio(); else stopAudio(); }
                if (restartVideo) {
                    camHandler.post(this::startVideo);
                } else if (j.has("torch")) {
                    camHandler.post(() -> { try { applyRequest(null); } catch (Exception ex) { lastError = "torch: " + ex.getMessage(); } });
                    sendHello();
                } else {
                    sendHello();
                }
                break;
            }
        }
    }

    /** One PC connection. Sends go through a queue so capture threads never block on the network. */
    class Client {
        final Socket s;
        final LinkedBlockingQueue<byte[]> q = new LinkedBlockingQueue<>();
        final AtomicLong queued = new AtomicLong();
        volatile boolean alive = true, authed = false, dropUntilKey = false;

        Client(Socket s) { this.s = s; }

        void start() {
            new Thread(this::readLoop, "client-read").start();
            new Thread(this::writeLoop, "client-write").start();
        }

        void send(int type, byte[] payload) {
            if (!alive) return;
            byte[] p = new byte[5 + payload.length];
            p[0] = (byte) type;
            p[1] = (byte) (payload.length >>> 24);
            p[2] = (byte) (payload.length >>> 16);
            p[3] = (byte) (payload.length >>> 8);
            p[4] = (byte) payload.length;
            System.arraycopy(payload, 0, p, 5, payload.length);
            queued.addAndGet(p.length);
            q.add(p);
        }

        void writeLoop() {
            try {
                s.setTcpNoDelay(true);
                DataOutputStream out = new DataOutputStream(new BufferedOutputStream(s.getOutputStream(), 65536));
                while (alive) {
                    byte[] p = q.poll(1, TimeUnit.SECONDS);
                    if (p == null) continue;
                    out.write(p);
                    queued.addAndGet(-p.length);
                    if (q.isEmpty()) out.flush();
                }
            } catch (Exception ignored) {
            } finally { close(); }
        }

        void readLoop() {
            try {
                s.setSoTimeout(30_000); // the PC pings every 5 s; silence means it is gone
                DataInputStream in = new DataInputStream(new BufferedInputStream(s.getInputStream()));
                while (alive) {
                    int type = in.readUnsignedByte();
                    int len = in.readInt();
                    if (len < 0 || len > 1_000_000) break;
                    byte[] payload = new byte[len];
                    in.readFully(payload);
                    if (!authed) {
                        if (type != T_CMD) break;
                        JSONObject j = new JSONObject(new String(payload, StandardCharsets.UTF_8));
                        String offered = j.optString("key");
                        String mine = prefs.getString("key", null);
                        if (mine == null && offered.length() >= 8) {
                            // Unpaired phone: trust the first PC that connects, and only that PC from now on.
                            prefs.edit().putString("key", offered).commit();
                            mine = offered;
                        }
                        if (!"auth".equals(j.optString("cmd")) || mine == null || !mine.equals(offered)) {
                            send(T_STATUS, "{\"error\":\"bad key\"}".getBytes(StandardCharsets.UTF_8));
                            SystemClock.sleep(300);
                            break;
                        }
                        authed = true;
                        // Only now take over from any previous PC connection.
                        Client old = client;
                        client = this;
                        if (old != null && old != this) old.close();
                        viewer = s.getInetAddress().getHostAddress();
                        updateNotification();
                        sendHello();
                        continue;
                    }
                    if (type == T_TALK) {
                        talkQ.add(payload);
                    } else if (type == T_CMD) {
                        handleCommand(new JSONObject(new String(payload, StandardCharsets.UTF_8)));
                    }
                }
            } catch (Exception ignored) {
            } finally { close(); }
        }

        void close() {
            if (!alive) return;
            alive = false;
            try { s.close(); } catch (IOException ignored) {}
            q.clear();
            if (client == this) {
                client = null;
                viewer = null;
                updateNotification();
            }
        }
    }

    // ------------------------------------------------------------------ helpers

    static void putLong(byte[] b, int off, long v) {
        for (int i = 7; i >= 0; i--) { b[off + i] = (byte) v; v >>>= 8; }
    }

    static String randomKey() {
        String abc = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
        SecureRandom r = new SecureRandom();
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < 8; i++) sb.append(abc.charAt(r.nextInt(abc.length())));
        return sb.toString();
    }

    static String wifiIp() {
        try {
            Enumeration<NetworkInterface> ifs = NetworkInterface.getNetworkInterfaces();
            while (ifs.hasMoreElements()) {
                NetworkInterface ni = ifs.nextElement();
                if (!ni.isUp() || ni.isLoopback()) continue;
                Enumeration<InetAddress> as = ni.getInetAddresses();
                while (as.hasMoreElements()) {
                    InetAddress a = as.nextElement();
                    if (a instanceof Inet4Address && ni.getName().startsWith("wlan")) return a.getHostAddress();
                }
            }
        } catch (Exception ignored) {}
        return null;
    }

    void updateNotification() {
        String text;
        if ("streaming".equals(camState)) text = viewer != null ? "Streaming to " + viewer : "Camera on, waiting for the viewer";
        else if ("error".equals(camState)) text = "Camera problem: " + lastError;
        else text = "Camera " + camState;
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        nm.notify(1, buildNotification(text));
    }

    Notification buildNotification(String text) {
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        Notification.Builder b;
        if (Build.VERSION.SDK_INT >= 26) {
            if (nm.getNotificationChannel("cam") == null) {
                NotificationChannel ch = new NotificationChannel("cam", "Camera streaming", NotificationManager.IMPORTANCE_LOW);
                ch.setShowBadge(false);
                nm.createNotificationChannel(ch);
            }
            b = new Notification.Builder(this, "cam");
        } else {
            b = new Notification.Builder(this);
        }
        Intent open = new Intent(this, MainActivity.class);
        PendingIntent pi = PendingIntent.getActivity(this, 0, open, PendingIntent.FLAG_IMMUTABLE);
        return b.setContentTitle("OldPhone CCTV")
                .setContentText(text)
                .setSmallIcon(R.drawable.ic_cam)
                .setOngoing(true)
                .setContentIntent(pi)
                .build();
    }
}
