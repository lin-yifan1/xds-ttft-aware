// MaaS 过载溯源插件 - 主动巡检版单文件 Java 实现 (proactive_main.py 的等价实现)
//
// 与 MaasPlugin.java（反应式 Mode A）平行的第二入口：由 maas-monitor 定时巡检
// 任务每 5 分钟逐服务调用，判断「此刻是否正在过载」，若是则定位根因租户并产出
// 按 (domain_id, resident_model_id, region) 维度的过载处理策略（strategies）。
//
// 入参（位置参数，顺序固定）：
//     1. service_id         infer_service_id（被巡检的池子/服务实例）
//     2. model_name         服务承载的模型名（用于 (P,M) 过滤 + SLA 选表 + 策略回填）
//     3. time               巡检时刻，ISO 8601 字符串或数字时间戳（秒/毫秒自动判定）
//     4. maasApiurl         MaaS 数据查询接口完整端点 URL
//     5. appcode            -> X-Apig-AppCode header
//     6. applydomainid      -> X-Apply-DomainID header
//     7. applyprojectid     -> X-Apply-ProjectID header
//
// 可选环境变量（覆盖默认）：
//     PLUGIN_TTFT_SLA / PLUGIN_TPOT_SLA / PLUGIN_ENABLE_TPOT / PLUGIN_SEVERE_RATIO /
//     PLUGIN_MILD_CONSECUTIVE_WINDOWS / PLUGIN_HISTORY_DAYS / PLUGIN_CANDIDATE_TOP_N /
//     PLUGIN_CULPRIT_TOP_K / PLUGIN_SCENARIO_TRIGGER_FACTOR / PLUGIN_DOMINANCE_MARGIN /
//     PLUGIN_TPM_CAP_FACTOR / PLUGIN_OUTPUT_CAP_FACTOR / PLUGIN_RPM_SHRINK_FACTOR /
//     PLUGIN_LOOKBACK_MINUTES / PLUGIN_ACTIVE_RECENT_MINUTES / PLUGIN_DETECT_ONLY /
//     PLUGIN_RETRY_MAX / PLUGIN_RETRY_BASE_SECONDS / PLUGIN_TIMEZONE
//
// 输出契约：stdout 一段多行 JSON；进度日志写 stderr。顶层 mode="proactive"，status 取值：
//     anomaly / normal / no_data / error (error 配合 exit code 1)
//     strategies 数组每行：{ domain_id, resident_model_id, region, process_type, value,
//                            model_name, project_id, scenario }
//     process_type 为协议原文：rpm_limit / tpm_limit / compeletion_token_limit
//     （compeletion 为公司接口与 DB 表的既定拼写，不可“修正”）。
//
// 编译运行 (JDK 11+)：
//     javac ProactivePlugin.java
//     java ProactivePlugin <service_id> <model_name> <time> <maasApiurl> <appcode> <applydomainid> <applyprojectid>

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.security.cert.X509Certificate;
import java.time.Duration;
import java.time.Instant;
import java.time.LocalDateTime;
import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.time.ZonedDateTime;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.TreeSet;
import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;

public class ProactivePlugin {

    static final double EPSILON = 1e-9;
    static final DateTimeFormatter ISO_MIN =
            DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mmxxx");

    // culprit 评分固定使用 both 权重（rpm / input / output 三维全参与）。
    // 主动巡检默认 TTFT-only 检测，scope 恒为 ttft_only；若沿用 scope 选权重会把
    // 输出维清零，导致 output_shift_dominant 场景不可达，故权重不再随 scope 变化。
    static final double[] SCORE_WEIGHTS = {0.28125, 0.34375, 0.375};

    // 触发指标顺序（影响并列时的稳定排序），以及指标 -> 场景类型 / 协议 process_type。
    static final String[] TRIGGER_METRICS = {"rpm", "tpm", "completion_tokens"};

    static String scenarioTypeForMetric(String metric) {
        switch (metric) {
            case "rpm": return "rpm_rise_dominant";
            case "tpm": return "tpm_rise_dominant";
            case "completion_tokens": return "output_shift_dominant";
            default: return "default";
        }
    }

    // 协议原文（含公司文档/接口/DB 一致的 compeletion 拼写），不可改。
    static String processForMetric(String metric) {
        switch (metric) {
            case "rpm": return "rpm_limit";
            case "tpm": return "tpm_limit";
            case "completion_tokens": return "compeletion_token_limit";
            default: return "rpm_limit";
        }
    }

    static String metricForProcess(String processType) {
        switch (processType) {
            case "rpm_limit": return "rpm";
            case "tpm_limit": return "tpm";
            case "compeletion_token_limit": return "completion_tokens";
            default: return "rpm";
        }
    }

    static final String DEFAULT_SCENARIO_TYPE = "default";

    // ========================================================================
    // 配置
    // ========================================================================
    static final class PluginConfig {
        Double ttftSlaOverride = null;   // null -> 按模型表
        Double tpotSlaOverride = null;
        boolean enableTpot = false;
        double severeRatio = 7.0;
        int mildConsecutiveWindows = 10;
        int eventMergeGap = 0;
        int maxEvents = 5;
        int minBaselinePoints = 6;
        int culpritTopK = 3;
        double culpritCumRatio = 0.8;
        double culpritMinRatio = 0.05;
        int historyDays = 14;
        int candidateTopN = 6;
        double scenarioTriggerFactor = 1.3;
        double dominanceMargin = 1.25;
        double tpmCapFactor = 1.5;
        double outputCapFactor = 1.5;
        double rpmShrinkFactor = 0.8;
        int lookbackMinutes = 60;
        int activeRecentMinutes = 5;
        boolean detectOnly = false;
        int retryMax = 3;
        double retryBaseSeconds = 2.0;
        int pageSize = 2000;
        double timeoutSeconds = 30.0;
        String timezone = "Asia/Shanghai";

        static PluginConfig loadFromEnv() {
            PluginConfig c = new PluginConfig();
            c.ttftSlaOverride = envFloatOptional("PLUGIN_TTFT_SLA");
            c.tpotSlaOverride = envFloatOptional("PLUGIN_TPOT_SLA");
            c.enableTpot = envBool("PLUGIN_ENABLE_TPOT", c.enableTpot);
            c.severeRatio = envFloat("PLUGIN_SEVERE_RATIO", c.severeRatio);
            c.mildConsecutiveWindows = envInt("PLUGIN_MILD_CONSECUTIVE_WINDOWS", c.mildConsecutiveWindows);
            c.historyDays = envInt("PLUGIN_HISTORY_DAYS", c.historyDays);
            c.candidateTopN = envInt("PLUGIN_CANDIDATE_TOP_N", c.candidateTopN);
            c.culpritTopK = envInt("PLUGIN_CULPRIT_TOP_K", c.culpritTopK);
            c.scenarioTriggerFactor = envFloat("PLUGIN_SCENARIO_TRIGGER_FACTOR", c.scenarioTriggerFactor);
            c.dominanceMargin = envFloat("PLUGIN_DOMINANCE_MARGIN", c.dominanceMargin);
            c.tpmCapFactor = envFloat("PLUGIN_TPM_CAP_FACTOR", c.tpmCapFactor);
            c.outputCapFactor = envFloat("PLUGIN_OUTPUT_CAP_FACTOR", c.outputCapFactor);
            c.rpmShrinkFactor = envFloat("PLUGIN_RPM_SHRINK_FACTOR", c.rpmShrinkFactor);
            c.lookbackMinutes = envInt("PLUGIN_LOOKBACK_MINUTES", c.lookbackMinutes);
            c.activeRecentMinutes = envInt("PLUGIN_ACTIVE_RECENT_MINUTES", c.activeRecentMinutes);
            c.detectOnly = envBool("PLUGIN_DETECT_ONLY", c.detectOnly);
            c.retryMax = envInt("PLUGIN_RETRY_MAX", c.retryMax);
            c.retryBaseSeconds = envFloat("PLUGIN_RETRY_BASE_SECONDS", c.retryBaseSeconds);
            c.timezone = envStr("PLUGIN_TIMEZONE", c.timezone);
            return c;
        }

        Map<String, Object> echo() {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("ttft_sla_override", ttftSlaOverride);
            m.put("tpot_sla_override", tpotSlaOverride);
            m.put("enable_tpot", enableTpot);
            m.put("severe_ratio", severeRatio);
            m.put("mild_consecutive_windows", mildConsecutiveWindows);
            m.put("event_merge_gap", eventMergeGap);
            m.put("max_events", maxEvents);
            m.put("min_baseline_points", minBaselinePoints);
            m.put("culprit_top_k", culpritTopK);
            m.put("culprit_cum_ratio", culpritCumRatio);
            m.put("culprit_min_ratio", culpritMinRatio);
            m.put("history_days", historyDays);
            m.put("candidate_top_n", candidateTopN);
            m.put("scenario_trigger_factor", scenarioTriggerFactor);
            m.put("dominance_margin", dominanceMargin);
            m.put("tpm_cap_factor", tpmCapFactor);
            m.put("output_cap_factor", outputCapFactor);
            m.put("rpm_shrink_factor", rpmShrinkFactor);
            m.put("lookback_minutes", lookbackMinutes);
            m.put("active_recent_minutes", activeRecentMinutes);
            m.put("detect_only", detectOnly);
            m.put("retry_max", retryMax);
            m.put("retry_base_seconds", retryBaseSeconds);
            m.put("page_size", pageSize);
            m.put("timeout_seconds", timeoutSeconds);
            m.put("timezone", timezone);
            return m;
        }
    }

    static double envFloat(String name, double def) {
        String raw = System.getenv(name);
        if (raw == null || raw.isEmpty()) return def;
        try { return Double.parseDouble(raw); } catch (Exception e) { return def; }
    }

    static Double envFloatOptional(String name) {
        String raw = System.getenv(name);
        if (raw == null || raw.isEmpty()) return null;
        try { return Double.parseDouble(raw); } catch (Exception e) { return null; }
    }

    static int envInt(String name, int def) {
        String raw = System.getenv(name);
        if (raw == null || raw.isEmpty()) return def;
        try { return Integer.parseInt(raw.trim()); } catch (Exception e) { return def; }
    }

    static boolean envBool(String name, boolean def) {
        String raw = System.getenv(name);
        if (raw == null || raw.isEmpty()) return def;
        String v = raw.trim().toLowerCase();
        return v.equals("1") || v.equals("true") || v.equals("yes") || v.equals("on");
    }

    static String envStr(String name, String def) {
        String raw = System.getenv(name);
        if (raw == null || raw.isEmpty()) return def;
        return raw;
    }

    // 模型化 SLA：模型名含 glm（忽略大小写）走 GLM 档，其余默认档；环境变量覆盖优先。
    static final class Sla {
        double ttft, tpot;
        String source;
        Sla(double ttft, double tpot, String source) { this.ttft = ttft; this.tpot = tpot; this.source = source; }
    }

    static Sla resolveSla(String modelName, PluginConfig cfg) {
        boolean glm = modelName != null && modelName.toLowerCase().contains("glm");
        double ttft = glm ? 30000.0 : 10000.0;
        double tpot = glm ? 500.0 : 150.0;
        String source = "model_table:" + (glm ? "glm" : "default");
        if (cfg.ttftSlaOverride != null) { ttft = cfg.ttftSlaOverride; source = "env_override"; }
        if (cfg.tpotSlaOverride != null) { tpot = cfg.tpotSlaOverride; source = "env_override"; }
        return new Sla(ttft, tpot, source);
    }

    // ========================================================================
    // 时间解析
    // ========================================================================
    static ZonedDateTime parseIsoReportedAt(String value, String defaultTz) {
        String text = value == null ? "" : value.trim();
        if (text.isEmpty()) throw new IllegalArgumentException("time argument is empty");
        ZoneId tz = ZoneId.of(defaultTz);
        try {
            double ts = Double.parseDouble(text);
            if (ts > 1e12) ts = ts / 1000.0;
            ZonedDateTime parsed = Instant.ofEpochSecond((long) ts).atZone(tz);
            return parsed.withSecond(0).withNano(0);
        } catch (NumberFormatException ignore) {
            // fall through
        }
        String normalized = text.replace("Z", "+00:00");
        ZonedDateTime parsed;
        try {
            parsed = OffsetDateTime.parse(normalized).toZonedDateTime();
        } catch (Exception e1) {
            try {
                LocalDateTime ldt = LocalDateTime.parse(normalized);
                parsed = ldt.atZone(tz);
            } catch (Exception e2) {
                throw new IllegalArgumentException(
                        "time argument is not valid ISO 8601 or timestamp: '" + value + "'");
            }
        }
        return parsed.withSecond(0).withNano(0);
    }

    static long toEpochMs(ZonedDateTime dt) {
        return dt.toInstant().toEpochMilli();
    }

    // ========================================================================
    // MaaS API 客户端
    // ========================================================================
    static final class MaasApiError extends RuntimeException {
        final Integer status;
        MaasApiError(String message, Integer status) { super(message); this.status = status; }
        MaasApiError(String message) { this(message, null); }
    }

    static List<Object> queryDimensions() {
        List<Object> dims = new ArrayList<>();
        dims.add(orderedMap("name", "timestamp", "granularity", "minute"));
        dims.add(orderedMap("name", "domain_id"));
        return dims;
    }

    // Round 3：按 (租户, 项目, 常驻服务, region, 池子) 拆分，用于 fan-out 推导与区域放大。
    static List<Object> r3Dimensions() {
        List<Object> dims = new ArrayList<>();
        dims.add(orderedMap("name", "timestamp", "granularity", "minute"));
        dims.add(orderedMap("name", "domain_id"));
        dims.add(orderedMap("name", "project_id"));
        dims.add(orderedMap("name", "resident_model_id"));
        dims.add(orderedMap("name", "region"));
        dims.add(orderedMap("name", "infer_service_id"));
        return dims;
    }

    static List<Object> queryMetrics() {
        List<Object> m = new ArrayList<>();
        String[][] specs = {
                {"ttft_avg", "avg"}, {"tpot_avg", "avg"}, {"success_cnt", "sum"},
                {"error_cnt", "sum"}, {"prompt_tokens", "avg"}, {"completion_tokens", "avg"},
                {"rpm", "sum"}, {"tpm", "sum"},
        };
        for (String[] s : specs) m.add(orderedMap("name", s[0], "func", s[1]));
        return m;
    }

    static Map<String, Object> orderedMap(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i + 1 < kv.length; i += 2) m.put((String) kv[i], kv[i + 1]);
        return m;
    }

    static final class MaasClient {
        final String url;
        final Map<String, String> headers = new LinkedHashMap<>();
        final double timeoutSeconds;
        final int pageSize;
        final int retryMax;
        final double retryBaseSeconds;
        final HttpClient client;
        int httpCallCount = 0;

        MaasClient(String url, String appcode, String applyDomainId, String applyProjectId,
                   double timeoutSeconds, int pageSize, int retryMax, double retryBaseSeconds) {
            if (url == null || url.isEmpty()) throw new IllegalArgumentException("maasApiurl is required");
            if (appcode == null || appcode.isEmpty()) throw new IllegalArgumentException("appcode is required");
            if (applyDomainId == null || applyDomainId.isEmpty())
                throw new IllegalArgumentException("applydomainid is required");
            if (applyProjectId == null || applyProjectId.isEmpty())
                throw new IllegalArgumentException("applyprojectid is required");
            this.url = url;
            headers.put("Content-Type", "application/json");
            headers.put("X-Apig-AppCode", appcode);
            headers.put("X-Apply-DomainID", applyDomainId);
            headers.put("X-Apply-ProjectID", applyProjectId);
            this.timeoutSeconds = timeoutSeconds;
            this.pageSize = Math.min(Math.max(pageSize, 1), 2000);
            this.retryMax = Math.max(retryMax, 0);
            this.retryBaseSeconds = Math.max(retryBaseSeconds, 0.0);
            this.client = buildInsecureClient(timeoutSeconds);
        }

        List<Map<String, Object>> query(List<Object> filters) {
            return query(filters, queryDimensions());
        }

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> query(List<Object> filters, List<Object> dimensions) {
            List<Map<String, Object>> rows = new ArrayList<>();
            int pageNum = 1;
            while (true) {
                Map<String, Object> payload = new LinkedHashMap<>();
                payload.put("dimensions", dimensions);
                payload.put("metrics", queryMetrics());
                payload.put("filters", filters);
                payload.put("page", orderedMap("pageNum", pageNum, "pageSize", pageSize));

                Map<String, Object> data = post(payload);
                Object listObj = data.get("list");
                if (listObj == null) listObj = new ArrayList<>();
                if (!(listObj instanceof List)) throw new MaasApiError("MaaS API list is not a list");
                List<Object> pageRows = (List<Object>) listObj;
                for (Object o : pageRows) {
                    if (o instanceof Map) rows.add((Map<String, Object>) o);
                }
                int pages = asInt(data.get("pages"), 1);
                int currentPage = asInt(data.get("pageNum"), pageNum);
                if (currentPage >= pages || pageRows.isEmpty()) break;
                pageNum = currentPage + 1;
            }
            return rows;
        }

        @SuppressWarnings("unchecked")
        Map<String, Object> post(Map<String, Object> payload) {
            String body = Json.write(payload);
            int attempt = 0;
            while (true) {
                HttpRequest.Builder rb = HttpRequest.newBuilder(URI.create(url))
                        .timeout(Duration.ofMillis((long) (timeoutSeconds * 1000)))
                        .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8));
                for (Map.Entry<String, String> e : headers.entrySet()) rb.header(e.getKey(), e.getValue());

                HttpResponse<String> resp;
                try {
                    resp = client.send(rb.build(), HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
                } catch (Exception exc) {
                    throw new MaasApiError("MaaS API request failed: " + exc);
                }
                httpCallCount++;
                // appcode 配额为 10 次/分钟，巡检多轮查询易触顶；429 做有界递增退避。
                if (resp.statusCode() == 429 && attempt < retryMax) {
                    attempt++;
                    double sleepSeconds = retryBaseSeconds * attempt;
                    logf("[http] 429 rate limited, retry %d/%d after %.1fs", attempt, retryMax, sleepSeconds);
                    try { Thread.sleep((long) (sleepSeconds * 1000)); } catch (InterruptedException ie) {
                        Thread.currentThread().interrupt();
                    }
                    continue;
                }
                if (resp.statusCode() != 200) {
                    String txt = resp.body() == null ? "" : resp.body();
                    if (txt.length() > 200) txt = txt.substring(0, 200);
                    throw new MaasApiError("MaaS API HTTP " + resp.statusCode() + ": " + txt, resp.statusCode());
                }
                Object parsed;
                try {
                    parsed = Json.parse(resp.body());
                } catch (Exception e) {
                    throw new MaasApiError("MaaS API returned invalid JSON");
                }
                if (!(parsed instanceof Map)) throw new MaasApiError("MaaS API response is not an object");
                Map<String, Object> body2 = (Map<String, Object>) parsed;
                int code = asInt(body2.get("code"), 200);
                if (code != 200) {
                    Object msg = body2.get("msg");
                    throw new MaasApiError("MaaS API code=" + code + " msg=" + (msg == null ? "" : msg), code);
                }
                return body2;
            }
        }
    }

    static HttpClient buildInsecureClient(double timeoutSeconds) {
        System.setProperty("jdk.internal.httpclient.disableHostnameVerification", "true");
        try {
            TrustManager[] trustAll = new TrustManager[]{new X509TrustManager() {
                public X509Certificate[] getAcceptedIssuers() { return new X509Certificate[0]; }
                public void checkClientTrusted(X509Certificate[] c, String a) { }
                public void checkServerTrusted(X509Certificate[] c, String a) { }
            }};
            SSLContext sc = SSLContext.getInstance("TLS");
            sc.init(null, trustAll, new SecureRandom());
            return HttpClient.newBuilder()
                    .sslContext(sc)
                    .connectTimeout(Duration.ofMillis((long) (timeoutSeconds * 1000)))
                    .build();
        } catch (Exception e) {
            throw new RuntimeException("failed to build HTTP client: " + e, e);
        }
    }

    static int asInt(Object o, int def) {
        if (o instanceof Number) return ((Number) o).intValue();
        if (o instanceof String) { try { return (int) Double.parseDouble((String) o); } catch (Exception e) { return def; } }
        return def;
    }

    // ========================================================================
    // API rows -> records
    // ========================================================================
    static final class Rec {
        String domainId;
        double rpm, tpm, ttft, tpot, prompt, completion;
        LocalDateTime time;
        // Round 3 拆分维度（仅 R3 查询填充，其余为空串）
        String projectId = "";
        String residentModelId = "";
        String region = "";
        String inferServiceId = "";
    }

    static double toFloat(Object value) {
        double out;
        if (value instanceof Number) out = ((Number) value).doubleValue();
        else if (value instanceof String) { try { out = Double.parseDouble(((String) value).trim()); } catch (Exception e) { return 0.0; } }
        else return 0.0;
        if (!Double.isFinite(out)) return 0.0;
        return out;
    }

    static LocalDateTime rowTimestampToLocal(Object value, ZoneId tz) {
        double raw;
        if (value instanceof Number) raw = ((Number) value).doubleValue();
        else if (value instanceof String) { try { raw = Double.parseDouble(((String) value).trim()); } catch (Exception e) { return null; } }
        else return null;
        if (raw > 1e12) raw = raw / 1000.0;
        try {
            return Instant.ofEpochSecond((long) raw).atZone(tz).toLocalDateTime().withSecond(0).withNano(0);
        } catch (Exception e) {
            return null;
        }
    }

    static String cell(Map<String, Object> row, String key) {
        Object v = row.get(key);
        return v == null ? "" : String.valueOf(v).trim();
    }

    static List<Rec> rowsToRecords(List<Map<String, Object>> rows, String tzName, boolean withR3Cols) {
        ZoneId tz = ZoneId.of(tzName);
        List<Rec> out = new ArrayList<>();
        for (Map<String, Object> row : rows) {
            String domainId = String.valueOf(row.getOrDefault("domain_id", "")).trim();
            if (domainId.isEmpty() || domainId.equals("null")) continue;
            if (row.containsKey("infer_service_id")) {
                String sid = cell(row, "infer_service_id");
                if (sid.isEmpty()) continue;
            }
            LocalDateTime ts = rowTimestampToLocal(row.get("timestamp"), tz);
            if (ts == null) continue;
            double success = toFloat(row.get("success_cnt"));
            double error = toFloat(row.get("error_cnt"));
            if (success + error <= 0) continue;
            Rec r = new Rec();
            r.domainId = domainId;
            r.rpm = toFloat(row.get("rpm"));
            r.tpm = toFloat(row.get("tpm"));
            r.ttft = toFloat(row.get("ttft_avg"));
            r.tpot = toFloat(row.get("tpot_avg"));
            r.prompt = toFloat(row.get("prompt_tokens"));
            r.completion = toFloat(row.get("completion_tokens"));
            r.time = ts;
            if (withR3Cols) {
                r.projectId = cell(row, "project_id");
                r.residentModelId = cell(row, "resident_model_id");
                r.region = cell(row, "region");
                r.inferServiceId = cell(row, "infer_service_id");
            }
            out.add(r);
        }
        return out;
    }

    // ========================================================================
    // 折叠重复行 + 排序
    // ========================================================================
    static final class Agg {
        String domainId; LocalDateTime time;
        double rpmSum, tpmSum;
        double ttftW, ttftWv, tpotW, tpotWv, promptW, promptWv, complW, complWv;
    }

    static List<Rec> prepareFrame(List<Rec> df) {
        if (df.isEmpty()) return new ArrayList<>();
        Map<String, Agg> groups = new LinkedHashMap<>();
        for (Rec r : df) {
            String key = r.domainId + " " + r.time.toString();
            Agg a = groups.get(key);
            if (a == null) { a = new Agg(); a.domainId = r.domainId; a.time = r.time; groups.put(key, a); }
            double w = (Double.isFinite(r.rpm) && r.rpm > 0) ? r.rpm : 0.0;
            a.rpmSum += r.rpm;
            a.tpmSum += r.tpm;
            if (Double.isFinite(r.ttft) && r.ttft != 0 && w > 0) { a.ttftW += w; a.ttftWv += r.ttft * w; }
            if (Double.isFinite(r.tpot) && r.tpot != 0 && w > 0) { a.tpotW += w; a.tpotWv += r.tpot * w; }
            if (Double.isFinite(r.prompt) && w > 0) { a.promptW += w; a.promptWv += r.prompt * w; }
            if (Double.isFinite(r.completion) && w > 0) { a.complW += w; a.complWv += r.completion * w; }
        }
        List<Rec> out = new ArrayList<>();
        for (Agg a : groups.values()) {
            Rec r = new Rec();
            r.domainId = a.domainId;
            r.time = a.time;
            r.rpm = a.rpmSum;
            r.tpm = a.tpmSum;
            r.ttft = a.ttftW > 0 ? a.ttftWv / a.ttftW : 0.0;
            r.tpot = a.tpotW > 0 ? a.tpotWv / a.tpotW : 0.0;
            r.prompt = a.promptW > 0 ? a.promptWv / a.promptW : 0.0;
            r.completion = a.complW > 0 ? a.complWv / a.complW : 0.0;
            out.add(r);
        }
        out.sort((x, y) -> {
            int c = x.domainId.compareTo(y.domainId);
            if (c != 0) return c;
            return x.time.compareTo(y.time);
        });
        return out;
    }

    // ========================================================================
    // 矩阵
    // ========================================================================
    static final class Matrices {
        double[][] rpm, tpm, ttft, tpot, prompt, completion;
    }

    static Matrices buildMetricMatrices(List<Rec> df, List<String> userIds, List<LocalDateTime> timeIndex) {
        int U = userIds.size(), T = timeIndex.size();
        Matrices m = new Matrices();
        m.rpm = new double[U][T]; m.tpm = new double[U][T];
        m.ttft = new double[U][T]; m.tpot = new double[U][T];
        m.prompt = new double[U][T]; m.completion = new double[U][T];
        Map<String, Integer> userPos = new LinkedHashMap<>();
        for (int i = 0; i < U; i++) userPos.put(userIds.get(i), i);
        Map<LocalDateTime, Integer> timePos = new LinkedHashMap<>();
        for (int i = 0; i < T; i++) timePos.put(timeIndex.get(i), i);
        for (Rec r : df) {
            Integer ui = userPos.get(r.domainId);
            Integer ti = timePos.get(r.time);
            if (ui == null || ti == null) continue;
            m.rpm[ui][ti] = r.rpm;
            m.tpm[ui][ti] = r.tpm;
            m.ttft[ui][ti] = r.ttft;
            m.tpot[ui][ti] = r.tpot;
            m.prompt[ui][ti] = r.prompt;
            m.completion[ui][ti] = r.completion;
        }
        return m;
    }

    static double[] columnSum(double[][] mat, int T) {
        double[] out = new double[T];
        for (double[] row : mat) for (int j = 0; j < T; j++) out[j] += row[j];
        return out;
    }

    static double weightedAvgIgnoreZeroCol(double[][] vals, double[][] weights, int col, int U) {
        double num = 0, den = 0;
        for (int u = 0; u < U; u++) {
            double v = vals[u][col], w = weights[u][col];
            if (Double.isFinite(v) && Double.isFinite(w) && w > 0 && v != 0) { num += v * w; den += w; }
        }
        return den > 0 ? num / den : 0.0;
    }

    static double weightedAvgCol(double[][] vals, double[][] weights, int col, int U) {
        double num = 0, den = 0;
        for (int u = 0; u < U; u++) {
            double v = vals[u][col], w = weights[u][col];
            if (Double.isFinite(v) && Double.isFinite(w) && w > 0) { num += v * w; den += w; }
        }
        return den > 0 ? num / den : 0.0;
    }

    static final class SystemSeries {
        double[] rpm, tpm, ttft, tpot, prompt, completion;
    }

    static SystemSeries buildSystemSeries(Matrices m, int T, int U) {
        SystemSeries s = new SystemSeries();
        s.rpm = columnSum(m.rpm, T);
        s.tpm = columnSum(m.tpm, T);
        s.ttft = new double[T]; s.tpot = new double[T];
        s.prompt = new double[T]; s.completion = new double[T];
        for (int i = 0; i < T; i++) {
            s.ttft[i] = weightedAvgIgnoreZeroCol(m.ttft, m.rpm, i, U);
            s.tpot[i] = weightedAvgIgnoreZeroCol(m.tpot, m.rpm, i, U);
            s.prompt[i] = weightedAvgCol(m.prompt, m.rpm, i, U);
            s.completion[i] = weightedAvgCol(m.completion, m.rpm, i, U);
        }
        return s;
    }

    // ========================================================================
    // 事件检测 + 活跃性
    // ========================================================================
    static boolean[] markRuns(boolean[] mask, int minRun) {
        if (minRun <= 1) return mask.clone();
        boolean[] out = new boolean[mask.length];
        int start = -1;
        for (int idx = 0; idx < mask.length; idx++) {
            if (mask[idx] && start < 0) start = idx;
            if (!mask[idx] && start >= 0) {
                if (idx - start >= minRun) for (int k = start; k < idx; k++) out[k] = true;
                start = -1;
            }
        }
        if (start >= 0 && (mask.length - start) >= minRun) for (int k = start; k < mask.length; k++) out[k] = true;
        return out;
    }

    static List<int[]> maskToEvents(boolean[] mask, int mergeGap) {
        List<Integer> idx = new ArrayList<>();
        for (int i = 0; i < mask.length; i++) if (mask[i]) idx.add(i);
        List<int[]> segments = new ArrayList<>();
        if (idx.isEmpty()) return segments;
        int start = idx.get(0), prev = idx.get(0);
        for (int k = 1; k < idx.size(); k++) {
            int h = idx.get(k);
            if (h <= prev + 1 + Math.max(mergeGap, 0)) { prev = h; continue; }
            segments.add(new int[]{start, prev});
            start = prev = h;
        }
        segments.add(new int[]{start, prev});
        return segments;
    }

    static List<int[]> capEvents(List<int[]> events, int maxEvents, double[] sysTtft, double[] sysTpot,
                                 double ttftSla, double tpotSla) {
        if (events.isEmpty() || maxEvents <= 0 || events.size() <= maxEvents) return events;
        List<double[]> scored = new ArrayList<>(); // [index, score]
        for (int i = 0; i < events.size(); i++) {
            int[] w = events.get(i);
            double ttftRatio = Double.NEGATIVE_INFINITY, tpotRatio = Double.NEGATIVE_INFINITY;
            for (int j = w[0]; j <= w[1]; j++) {
                ttftRatio = Math.max(ttftRatio, sysTtft[j] / Math.max(ttftSla, EPSILON));
                tpotRatio = Math.max(tpotRatio, sysTpot[j] / Math.max(tpotSla, EPSILON));
            }
            scored.add(new double[]{i, Math.max(ttftRatio, tpotRatio)});
        }
        scored.sort((x, y) -> Double.compare(y[1], x[1]));
        List<int[]> out = new ArrayList<>();
        for (int i = 0; i < maxEvents; i++) out.add(events.get((int) scored.get(i)[0]));
        return out;
    }

    static final class EventInfo {
        boolean[] sysAnom, sysAnomTtft, sysAnomTpot;
        List<int[]> events;
    }

    static EventInfo detectSystemEvents(PluginConfig cfg, double[] sysTtft, double[] sysTpot,
                                        double ttftSla, double tpotSla) {
        int T = sysTtft.length;
        boolean[] ttftHeavy = new boolean[T], ttftMild = new boolean[T];
        for (int i = 0; i < T; i++) {
            ttftHeavy[i] = sysTtft[i] >= ttftSla * cfg.severeRatio;
            ttftMild[i] = sysTtft[i] > ttftSla;
        }
        boolean[] ttftRun = markRuns(ttftMild, cfg.mildConsecutiveWindows);
        EventInfo info = new EventInfo();
        info.sysAnomTtft = new boolean[T]; info.sysAnomTpot = new boolean[T]; info.sysAnom = new boolean[T];
        boolean[] tpotRun = null;
        boolean[] tpotHeavy = new boolean[T];
        if (cfg.enableTpot) {
            boolean[] tpotMild = new boolean[T];
            for (int i = 0; i < T; i++) {
                tpotHeavy[i] = sysTpot[i] >= tpotSla * cfg.severeRatio;
                tpotMild[i] = sysTpot[i] > tpotSla;
            }
            tpotRun = markRuns(tpotMild, cfg.mildConsecutiveWindows);
        }
        for (int i = 0; i < T; i++) {
            info.sysAnomTtft[i] = ttftHeavy[i] || ttftRun[i];
            // 本轮公司范围仅 TTFT 参与检测；TPOT 序列仍输出诊断统计。
            info.sysAnomTpot[i] = cfg.enableTpot && (tpotHeavy[i] || tpotRun[i]);
            info.sysAnom[i] = info.sysAnomTtft[i] || info.sysAnomTpot[i];
        }
        List<int[]> events = maskToEvents(info.sysAnom, cfg.eventMergeGap);
        events = capEvents(events, cfg.maxEvents, sysTtft, sysTpot, ttftSla, tpotSla);
        events.sort((x, y) -> Integer.compare(x[0], y[0]));
        info.events = events;
        return info;
    }

    static String scopeForWindow(boolean[] sysAnomTtft, boolean[] sysAnomTpot, int a, int b) {
        boolean ttftActive = false, tpotActive = false;
        for (int i = a; i <= b; i++) { if (sysAnomTtft[i]) ttftActive = true; if (sysAnomTpot[i]) tpotActive = true; }
        if (ttftActive && tpotActive) return "both";
        if (ttftActive) return "ttft_only";
        return "tpot_only";
    }

    // 活跃性规则：事件末端落在窗口最后 active_recent_minutes 分钟内才算「正在过载」。
    // 多个活跃事件时取末端最新者，再以 TTFT 峰值比破并列。
    static int[] selectActiveEvent(PluginConfig cfg, List<int[]> events, int pointCount,
                                   double[] sysTtft, double ttftSla) {
        int threshold = pointCount - Math.max(cfg.activeRecentMinutes, 1);
        int[] best = null;
        double bestPeak = Double.NEGATIVE_INFINITY;
        for (int[] ev : events) {
            if (ev[1] < threshold) continue;
            double peak = Double.NEGATIVE_INFINITY;
            for (int j = ev[0]; j <= ev[1]; j++) peak = Math.max(peak, sysTtft[j]);
            peak = peak / Math.max(ttftSla, EPSILON);
            if (best == null || ev[1] > best[1] || (ev[1] == best[1] && peak > bestPeak)) {
                best = ev; bestPeak = peak;
            }
        }
        return best;
    }

    // ========================================================================
    // 评分辅助
    // ========================================================================
    static double[] safeRatio(double[] values) {
        double total = 0;
        for (double v : values) total += v;
        double[] out = new double[values.length];
        if (total <= 0) return out;
        for (int i = 0; i < values.length; i++) out[i] = values[i] / total;
        return out;
    }

    static double[] combinedLocalScore(double[] rpmExcess, double[] promptDelta, double[] complDelta, double[] weights) {
        double wRpm = weights[0], wInput = weights[1], wOutput = weights[2];
        int n = rpmExcess.length;
        double[] score = new double[n];
        double tRpm = sum(rpmExcess), tIn = sum(promptDelta), tOut = sum(complDelta);
        for (int i = 0; i < n; i++) {
            if (tRpm > 0) score[i] += wRpm * (rpmExcess[i] / tRpm);
            if (tIn > 0) score[i] += wInput * (promptDelta[i] / tIn);
            if (tOut > 0) score[i] += wOutput * (complDelta[i] / tOut);
        }
        return score;
    }

    static double windowMean(double[] values) {
        double s = 0; int n = 0;
        for (double v : values) if (Double.isFinite(v) && v > 0) { s += v; n++; }
        return n == 0 ? 0.0 : s / n;
    }

    static Double baselineScalar(double[] values) {
        double s = 0; int n = 0;
        for (double v : values) if (Double.isFinite(v) && v > 0) { s += v; n++; }
        return n == 0 ? null : s / n;
    }

    // ========================================================================
    // 场景分类（dominant + margin）与池级杠杆
    // ========================================================================
    static final class Classify {
        Map<String, Object> scenario; // null 表示零触发
        Map<String, Double> ratios = new LinkedHashMap<>();
        List<String> triggered = new ArrayList<>();
        List<String> suppressed = new ArrayList<>();
    }

    static Classify classifyScenario(PluginConfig cfg, Map<String, Double> current, Map<String, Double> baseline) {
        Classify res = new Classify();
        for (String metric : TRIGGER_METRICS) {
            Double bl = baseline.get(metric);
            double cur = current.getOrDefault(metric, 0.0);
            if (bl == null || bl <= 0) { res.suppressed.add(metric); continue; }
            res.ratios.put(metric, cur / bl);
        }
        for (String metric : TRIGGER_METRICS) {
            Double r = res.ratios.get(metric);
            if (r != null && r >= cfg.scenarioTriggerFactor) res.triggered.add(metric);
        }
        if (res.triggered.isEmpty()) return res;

        String stype, ptype, decision;
        if (res.triggered.size() == 1) {
            String metric = res.triggered.get(0);
            stype = scenarioTypeForMetric(metric); ptype = processForMetric(metric); decision = "single";
        } else {
            List<String> ordered = new ArrayList<>(res.triggered);
            // 稳定排序：ratio 降序，并列保留 TRIGGER_METRICS 原顺序
            Collections.sort(ordered, (x, y) -> Double.compare(res.ratios.get(y), res.ratios.get(x)));
            String top = ordered.get(0), runner = ordered.get(1);
            if (res.ratios.get(top) >= res.ratios.get(runner) * cfg.dominanceMargin) {
                stype = scenarioTypeForMetric(top); ptype = processForMetric(top); decision = "margin";
            } else {
                stype = DEFAULT_SCENARIO_TYPE; ptype = "rpm_limit"; decision = "mixed";
            }
        }
        Map<String, Object> scenario = new LinkedHashMap<>();
        scenario.put("type", stype);
        scenario.put("process_type", ptype);
        scenario.put("decision", decision);
        Map<String, Object> triggerRatios = new LinkedHashMap<>();
        for (Map.Entry<String, Double> e : res.ratios.entrySet()) triggerRatios.put(e.getKey(), e.getValue());
        scenario.put("trigger_ratios", triggerRatios);
        scenario.put("triggered", new ArrayList<Object>(res.triggered));
        res.scenario = scenario;
        return res;
    }

    // 单指标杠杆。rpm/tpm 为 rate_scale（须 s<1 才有效）；completion 为 length_cap。
    static Map<String, Object> leverForMetric(PluginConfig cfg, String metric,
                                              Map<String, Double> current, Map<String, Double> baseline) {
        Double bl = baseline.get(metric);
        if (bl == null || bl <= 0) return null;
        if (metric.equals("completion_tokens")) {
            double cap = bl * cfg.outputCapFactor;
            Map<String, Object> lev = new LinkedHashMap<>();
            lev.put("metric", metric);
            lev.put("kind", "length_cap");
            lev.put("baseline", bl);
            lev.put("factor", cfg.outputCapFactor);
            lev.put("cap", cap);
            return lev;
        }
        double cur = current.getOrDefault(metric, 0.0);
        if (cur <= 0) return null;
        double factor = metric.equals("rpm") ? cfg.rpmShrinkFactor : cfg.tpmCapFactor;
        double target = bl * factor;
        double s = Math.min(target / cur, 1.0);
        if (s >= 1.0) return null;
        Map<String, Object> lev = new LinkedHashMap<>();
        lev.put("metric", metric);
        lev.put("kind", "rate_scale");
        lev.put("current", cur);
        lev.put("baseline", bl);
        lev.put("factor", factor);
        lev.put("target", target);
        lev.put("s", s);
        return lev;
    }

    static final class LeverResult {
        Map<String, Object> lever; // null
        String processType;
        String note;     // null
        String warning;  // null
    }

    // 场景 -> 池级杠杆。dominant 场景只评估自身杠杆；default(mixed) 落 rpm_limit，
    // rpm 不可降时沿触发指标按 ratio 降序兜底到次优杠杆（process_type 随之切换并打 warning）。
    static LeverResult computePoolLever(PluginConfig cfg, Map<String, Object> scenario,
                                        Map<String, Double> current, Map<String, Double> baseline,
                                        Map<String, Double> ratios, List<String> triggered) {
        LeverResult out = new LeverResult();
        String stype = (String) scenario.get("type");
        List<String> chain = new ArrayList<>();
        if (DEFAULT_SCENARIO_TYPE.equals(stype)) {
            chain.add("rpm");
            List<String> rest = new ArrayList<>();
            for (String m : triggered) if (!m.equals("rpm")) rest.add(m);
            Collections.sort(rest, (x, y) -> Double.compare(ratios.getOrDefault(y, 0.0), ratios.getOrDefault(x, 0.0)));
            chain.addAll(rest);
        } else {
            chain.add(metricForProcess((String) scenario.get("process_type")));
        }

        List<String> attempts = new ArrayList<>();
        for (String metric : chain) {
            Map<String, Object> lever = leverForMetric(cfg, metric, current, baseline);
            if (lever != null) {
                String ptype = processForMetric(metric);
                String warning = null;
                if (DEFAULT_SCENARIO_TYPE.equals(stype) && !metric.equals("rpm")) {
                    warning = "default_fallback_to_" + ptype + ": " + String.join("; ", attempts);
                }
                out.lever = lever; out.processType = ptype; out.note = null; out.warning = warning;
                return out;
            }
            attempts.add(metric + "_not_reducible_or_baseline_missing");
        }
        out.lever = null;
        out.processType = (String) scenario.get("process_type");
        out.note = "lever_not_computable: " + String.join("; ", attempts);
        out.warning = null;
        return out;
    }

    // ========================================================================
    // 跨池 baseline (同时刻偏移均值)
    // ========================================================================
    static int offsetFromAnchor(LocalDateTime t, LocalDateTime reported) {
        long anchorSeconds = reported.getHour() * 3600L + reported.getMinute() * 60L + reported.getSecond();
        LocalDateTime anchor = t.toLocalDate().atStartOfDay().plusSeconds(anchorSeconds);
        long delta = Duration.between(anchor, t).getSeconds();
        return (int) Math.floorDiv(delta, 60L);
    }

    static final class Baselines {
        double[][] rpm, tpm, prompt, completion;
    }

    static Baselines historicalBaselinesByOffset(PluginConfig cfg, List<Rec> history, List<String> userIds,
                                                 List<LocalDateTime> timeIndex, LocalDateTime reported) {
        int U = userIds.size(), T = timeIndex.size();
        Baselines b = new Baselines();
        b.rpm = new double[U][T]; b.tpm = new double[U][T]; b.prompt = new double[U][T]; b.completion = new double[U][T];
        if (history.isEmpty() || U == 0 || T == 0) return b;

        Map<String, Integer> userPos = new LinkedHashMap<>();
        for (int i = 0; i < U; i++) userPos.put(userIds.get(i), i);
        int[] currentOffsets = new int[T];
        for (int i = 0; i < T; i++) currentOffsets[i] = offsetFromAnchor(timeIndex.get(i), reported);

        final class GAgg { int count; double rpm, tpm, prompt, completion; }
        Map<String, GAgg> groups = new LinkedHashMap<>();
        Map<String, String> groupDomain = new LinkedHashMap<>();
        Map<String, Integer> groupOffset = new LinkedHashMap<>();
        for (Rec r : history) {
            int off = offsetFromAnchor(r.time, reported);
            String key = r.domainId + " " + off;
            GAgg g = groups.get(key);
            if (g == null) { g = new GAgg(); groups.put(key, g); groupDomain.put(key, r.domainId); groupOffset.put(key, off); }
            g.count++;
            g.rpm += r.rpm; g.tpm += r.tpm; g.prompt += r.prompt; g.completion += r.completion;
        }
        for (Map.Entry<String, GAgg> e : groups.entrySet()) {
            GAgg g = e.getValue();
            if (g.count < cfg.minBaselinePoints) continue;
            Integer uidPos = userPos.get(groupDomain.get(e.getKey()));
            if (uidPos == null) continue;
            int off = groupOffset.get(e.getKey());
            double meanRpm = g.rpm / g.count, meanTpm = g.tpm / g.count;
            double meanPrompt = g.prompt / g.count, meanCompl = g.completion / g.count;
            for (int col = 0; col < T; col++) {
                if (currentOffsets[col] == off) {
                    b.rpm[uidPos][col] = meanRpm;
                    b.tpm[uidPos][col] = meanTpm;
                    b.prompt[uidPos][col] = meanPrompt;
                    b.completion[uidPos][col] = meanCompl;
                }
            }
        }
        return b;
    }

    // ========================================================================
    // 候选选择
    // ========================================================================
    static List<String> pickCandidates(PluginConfig cfg, List<String> userIds, Matrices m, int a, int b,
                                       double ttftSla, double tpotSla) {
        int U = userIds.size();
        double[] candScore = new double[U];
        for (int u = 0; u < U; u++) {
            double ttftMax = 0, tpotMax = 0;
            for (int j = a; j <= b; j++) { ttftMax = Math.max(ttftMax, m.ttft[u][j]); tpotMax = Math.max(tpotMax, m.tpot[u][j]); }
            candScore[u] = ttftMax / Math.max(ttftSla, EPSILON) + tpotMax / Math.max(tpotSla, EPSILON);
        }
        Integer[] order = new Integer[U];
        for (int i = 0; i < U; i++) order[i] = i;
        Arrays.sort(order, (x, y) -> Double.compare(candScore[y], candScore[x]));
        List<String> chosen = new ArrayList<>();
        TreeSet<String> seen = new TreeSet<>();
        for (int idx : order) {
            if (chosen.size() >= cfg.candidateTopN) break;
            String uid = userIds.get(idx);
            if (seen.contains(uid)) continue;
            chosen.add(uid); seen.add(uid);
        }
        return chosen;
    }

    // ========================================================================
    // Round 3：region/常驻服务拆分与区域放大
    // ========================================================================
    static final class Breakdown {
        List<Map<String, Object>> rows;
        String note; // null 表示成功
        Breakdown(List<Map<String, Object>> rows, String note) { this.rows = rows; this.note = note; }
    }

    static Breakdown buildRegionBreakdown(List<Rec> r3, String domainId, String serviceId, Map<String, Object> lever) {
        if (r3.isEmpty()) return new Breakdown(new ArrayList<>(), "resident_breakdown_unavailable");
        // sub = 该租户 + 非空 (resident, region)
        List<Rec> sub = new ArrayList<>();
        for (Rec r : r3) {
            if (!r.domainId.equals(domainId)) continue;
            if (r.residentModelId.isEmpty() || r.region.isEmpty()) continue;
            sub.add(r);
        }
        if (sub.isEmpty()) return new Breakdown(new ArrayList<>(), "resident_breakdown_unavailable");

        // fan-out = 路由到过载池 P 的 (resident, region)，按 (resident, region) 排序
        TreeSet<String> fanout = new TreeSet<>();
        for (Rec r : sub) {
            if (r.inferServiceId.equals(serviceId)) fanout.add(r.residentModelId + " " + r.region);
        }
        if (fanout.isEmpty()) return new Breakdown(new ArrayList<>(), "resident_breakdown_unavailable");

        String kind = (String) lever.get("kind");
        List<Map<String, Object>> rows = new ArrayList<>();
        for (String pair : fanout) {
            int sep = pair.indexOf(' ');
            String resident = pair.substring(0, sep);
            String region = pair.substring(sep + 1);

            // grp = sub 中匹配 (resident, region) 的行
            List<Rec> grp = new ArrayList<>();
            for (Rec r : sub) if (r.residentModelId.equals(resident) && r.region.equals(region)) grp.add(r);

            Map<String, Object> entry = new LinkedHashMap<>();
            entry.put("resident_model_id", resident);
            entry.put("region", region);

            if ("rate_scale".equals(kind)) {
                String metric = (String) lever.get("metric");
                // 逐分钟对池子求和，再取非零均值
                Map<LocalDateTime, Double> perMinute = new LinkedHashMap<>();
                for (Rec r : grp) {
                    double v = metric.equals("tpm") ? r.tpm : r.rpm;
                    perMinute.merge(r.time, v, Double::sum);
                }
                double[] sums = new double[perMinute.size()];
                int i = 0;
                for (double v : perMinute.values()) sums[i++] = v;
                double regionTotal = windowMean(sums);
                if (regionTotal <= 0) continue;
                double s = (Double) lever.get("s");
                entry.put("region_total", regionTotal);
                entry.put("s", s);
                entry.put("value", Math.max((long) (regionTotal * s), 1L));
            } else {
                double cap = (Double) lever.get("cap");
                entry.put("value", Math.max((long) cap, 1L));
            }
            entry.put("project_id", dominantProject(grp));
            rows.add(entry);
        }
        if (rows.isEmpty()) return new Breakdown(new ArrayList<>(), "resident_breakdown_unavailable");
        return new Breakdown(rows, null);
    }

    // 按 rpm 份额最大的 project_id；并列时取 project_id 字典序最小（对齐 pandas idxmax）。
    static String dominantProject(List<Rec> grp) {
        TreeMap<String, Double> projSums = new TreeMap<>();
        for (Rec r : grp) projSums.merge(r.projectId, r.rpm, Double::sum);
        if (projSums.isEmpty()) return "";
        String best = "";
        double bestVal = Double.NEGATIVE_INFINITY;
        for (Map.Entry<String, Double> e : projSums.entrySet()) {
            if (e.getValue() > bestVal) { bestVal = e.getValue(); best = e.getKey(); }
        }
        return best;
    }

    // ========================================================================
    // 系统统计 + 输出基础
    // ========================================================================
    static Map<String, Object> block(double[] values, Double sla, PluginConfig cfg) {
        Map<String, Object> b = new LinkedHashMap<>();
        b.put("system_avg", values.length > 0 ? mean(values) : 0.0);
        b.put("system_p95", values.length > 0 ? quantile(values, 0.95) : 0.0);
        b.put("system_max", values.length > 0 ? max(values) : 0.0);
        if (sla != null) {
            b.put("sla", sla);
            b.put("severe_threshold", sla * cfg.severeRatio);
        }
        return b;
    }

    static Map<String, Object> systemStats(PluginConfig cfg, SystemSeries s, boolean[] sysAnom, int eventCount,
                                           double ttftSla, double tpotSla) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("minutes", s.ttft.length);
        m.put("event_count", eventCount);
        int anomCount = 0;
        for (boolean v : sysAnom) if (v) anomCount++;
        m.put("system_anom_minutes_count", anomCount);
        m.put("ttft", block(s.ttft, ttftSla, cfg));
        m.put("tpot", block(s.tpot, tpotSla, cfg));
        m.put("rpm", block(s.rpm, null, cfg));
        m.put("tpm", block(s.tpm, null, cfg));
        m.put("prompt_tokens", block(s.prompt, null, cfg));
        m.put("completion_tokens", block(s.completion, null, cfg));
        return m;
    }

    static String formatTs(List<LocalDateTime> timeIndex, int idx, ZoneId tz) {
        return timeIndex.get(idx).atZone(tz).format(ISO_MIN);
    }

    static Map<String, Object> makeBaseOutput(String status, PluginConfig cfg, String serviceId, String modelName,
                                              ZonedDateTime checkedAt, double ttftSla, double tpotSla, String slaSource) {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("status", status);
        out.put("mode", "proactive");
        Map<String, Object> sweep = new LinkedHashMap<>();
        sweep.put("service_id", serviceId);
        sweep.put("model_name", modelName);
        sweep.put("checked_at", checkedAt.format(ISO_MIN));
        sweep.put("lookback_minutes", cfg.lookbackMinutes);
        sweep.put("active_recent_minutes", cfg.activeRecentMinutes);
        sweep.put("detect_only", cfg.detectOnly);
        out.put("sweep", sweep);
        Map<String, Object> sla = new LinkedHashMap<>();
        sla.put("ttft_sla", ttftSla);
        sla.put("tpot_sla", tpotSla);
        sla.put("tpot_detection_enabled", cfg.enableTpot);
        sla.put("source", slaSource);
        out.put("sla", sla);
        out.put("config_echo", cfg.echo());
        return out;
    }

    // ========================================================================
    // 简单数学
    // ========================================================================
    static double sum(double[] a) { double s = 0; for (double v : a) s += v; return s; }
    static double mean(double[] a) { return a.length == 0 ? 0.0 : sum(a) / a.length; }
    static double max(double[] a) { double m = Double.NEGATIVE_INFINITY; for (double v : a) m = Math.max(m, v); return m; }

    static int argmax(double[] a) {
        int best = 0; double bv = Double.NEGATIVE_INFINITY;
        for (int i = 0; i < a.length; i++) if (a[i] > bv) { bv = a[i]; best = i; }
        return best;
    }

    static int argmaxRange(double[] a, int from, int to) {
        int best = 0; double bv = Double.NEGATIVE_INFINITY;
        for (int i = from; i <= to; i++) if (a[i] > bv) { bv = a[i]; best = i - from; }
        return best;
    }

    static double[] slice(double[] a, int from, int to) {
        return Arrays.copyOfRange(a, from, to + 1);
    }

    static double quantile(double[] a, double q) {
        double[] s = a.clone();
        Arrays.sort(s);
        int n = s.length;
        if (n == 1) return s[0];
        double pos = q * (n - 1);
        int lo = (int) Math.floor(pos);
        int hi = (int) Math.ceil(pos);
        double frac = pos - lo;
        return s[lo] + frac * (s[hi] - s[lo]);
    }

    static int emit(Map<String, Object> payload, int exitCode) {
        System.out.println(Json.write(payload));
        return exitCode;
    }

    static void logf(String fmt, Object... args) {
        System.err.println(String.format(fmt, args));
    }

    // ========================================================================
    // 主流程
    // ========================================================================
    static final class Result { Map<String, Object> payload; int exitCode; Result(Map<String, Object> p, int e) { payload = p; exitCode = e; } }

    static Result runPlugin(String serviceId, String modelName, String timeIso, String maasUrl,
                            String appcode, String applyDomainId, String applyProjectId) {
        PluginConfig cfg = PluginConfig.loadFromEnv();
        ZoneId tz = ZoneId.of(cfg.timezone);
        ZonedDateTime checkedAt = parseIsoReportedAt(timeIso, cfg.timezone);
        LocalDateTime checkedNaive = checkedAt.toLocalDateTime();
        if (modelName == null || modelName.trim().isEmpty()) throw new IllegalArgumentException("model_name is required");
        modelName = modelName.trim();
        Sla slaInfo = resolveSla(modelName, cfg);
        double ttftSla = slaInfo.ttft, tpotSla = slaInfo.tpot;

        MaasClient client = new MaasClient(maasUrl, appcode, applyDomainId, applyProjectId,
                cfg.timeoutSeconds, cfg.pageSize, cfg.retryMax, cfg.retryBaseSeconds);

        // ---- Round 1: (P, M) 巡检时刻前 lookback 分钟 ----
        ZonedDateTime r1Start = checkedAt.minusMinutes(cfg.lookbackMinutes);
        ZonedDateTime r1EndInclusive = checkedAt.minusMinutes(1);
        logf("[round1] sweep service=%s model=%s window=%s~%s", serviceId, modelName,
                r1Start.format(ISO_MIN), checkedAt.format(ISO_MIN));
        List<Object> r1Filters = new ArrayList<>();
        r1Filters.add(orderedMap("name", "infer_service_id", "operator", "=", "value", serviceId));
        r1Filters.add(orderedMap("name", "model_name", "operator", "=", "value", modelName));
        r1Filters.add(orderedMap("name", "timestamp", "operator", ">=", "value", String.valueOf(toEpochMs(r1Start))));
        r1Filters.add(orderedMap("name", "timestamp", "operator", "<=", "value", String.valueOf(toEpochMs(r1EndInclusive))));
        List<Map<String, Object>> r1Rows = client.query(r1Filters);
        logf("[round1] rows=%d", r1Rows.size());
        List<Rec> r1Df = rowsToRecords(r1Rows, cfg.timezone, false);
        logf("[round1] df_rows=%d after filter", r1Df.size());

        Map<String, Object> base = makeBaseOutput("normal", cfg, serviceId, modelName, checkedAt, ttftSla, tpotSla, slaInfo.source);

        if (r1Df.isEmpty()) {
            base.put("status", "no_data");
            base.put("events", new ArrayList<>());
            base.put("culprits", new ArrayList<>());
            base.put("strategies", new ArrayList<>());
            base.put("api_call_count", client.httpCallCount);
            return new Result(base, 0);
        }

        List<Rec> prepared = prepareFrame(r1Df);
        TreeSet<String> uidSet = new TreeSet<>();
        for (Rec r : prepared) uidSet.add(r.domainId);
        List<String> userIds = new ArrayList<>(uidSet);
        LocalDateTime windowStartNaive = checkedNaive.minusMinutes(cfg.lookbackMinutes);
        List<LocalDateTime> timeIndex = new ArrayList<>();
        for (int i = 0; i < cfg.lookbackMinutes; i++) timeIndex.add(windowStartNaive.plusMinutes(i));
        int pointCount = timeIndex.size();

        Matrices matrices = buildMetricMatrices(prepared, userIds, timeIndex);
        int U = userIds.size(), T = timeIndex.size();
        SystemSeries system = buildSystemSeries(matrices, T, U);
        EventInfo eventInfo = detectSystemEvents(cfg, system.ttft, system.tpot, ttftSla, tpotSla);
        List<int[]> events = eventInfo.events;

        base.put("system_stats", systemStats(cfg, system, eventInfo.sysAnom, events.size(), ttftSla, tpotSla));

        int[] activeEvent = selectActiveEvent(cfg, events, pointCount, system.ttft, ttftSla);
        if (activeEvent == null) {
            base.put("events", new ArrayList<>());
            base.put("culprits", new ArrayList<>());
            base.put("strategies", new ArrayList<>());
            base.put("inactive_event_count", events.size());
            base.put("api_call_count", client.httpCallCount);
            return new Result(base, 0);
        }

        int a = activeEvent[0], b = activeEvent[1];
        String scope = scopeForWindow(eventInfo.sysAnomTtft, eventInfo.sysAnomTpot, a, b);
        int sysPeakTtftOffset = argmaxRange(system.ttft, a, b);
        int sysPeakTpotOffset = argmaxRange(system.tpot, a, b);
        Map<String, Object> eventPayload = new LinkedHashMap<>();
        eventPayload.put("start", formatTs(timeIndex, a, tz));
        eventPayload.put("end", formatTs(timeIndex, b, tz));
        eventPayload.put("duration_minutes", b - a + 1);
        eventPayload.put("scope", scope);
        eventPayload.put("system_peak_ttft_time", formatTs(timeIndex, a + sysPeakTtftOffset, tz));
        eventPayload.put("system_peak_ttft", system.ttft[a + sysPeakTtftOffset]);
        eventPayload.put("system_peak_tpot_time", formatTs(timeIndex, a + sysPeakTpotOffset, tz));
        eventPayload.put("system_peak_tpot", system.tpot[a + sysPeakTpotOffset]);

        base.put("status", "anomaly");
        List<Object> evList = new ArrayList<>();
        evList.add(eventPayload);
        base.put("events", evList);

        // ---- detect-only：恢复巡检只要「还过载吗」，省掉 Round 2/3 ----
        if (cfg.detectOnly) {
            base.put("culprits", new ArrayList<>());
            base.put("strategies", new ArrayList<>());
            base.put("note", "detect_only");
            base.put("api_call_count", client.httpCallCount);
            return new Result(base, 0);
        }

        // ---- 候选选择 ----
        List<String> candidates = pickCandidates(cfg, userIds, matrices, a, b, ttftSla, tpotSla);
        logf("[round1] active event=[%d,%d] scope=%s candidates=%d uids=%s", a, b, scope, candidates.size(), candidates);

        // ---- Round 2: 跨池 14d 同时刻偏移基线（范围止于当前窗口之前，天然不混入当前数据）----
        ZonedDateTime r2Start = r1Start.minusDays(cfg.historyDays);
        ZonedDateTime r2EndInclusive = r1Start.minusMinutes(1);
        logf("[round2] query history candidates=%d window=%s~%s", candidates.size(),
                r2Start.format(ISO_MIN), r2EndInclusive.format(ISO_MIN));
        List<Object> r2Filters = new ArrayList<>();
        r2Filters.add(orderedMap("name", "domain_id", "operator", "in", "value", new ArrayList<Object>(candidates)));
        r2Filters.add(orderedMap("name", "model_name", "operator", "=", "value", modelName));
        r2Filters.add(orderedMap("name", "timestamp", "operator", ">=", "value", String.valueOf(toEpochMs(r2Start))));
        r2Filters.add(orderedMap("name", "timestamp", "operator", "<=", "value", String.valueOf(toEpochMs(r2EndInclusive))));
        List<Map<String, Object>> r2Rows = client.query(r2Filters);
        List<Rec> r2Df = rowsToRecords(r2Rows, cfg.timezone, false);
        List<Rec> historyPrepared = r2Df.isEmpty() ? r2Df : prepareFrame(r2Df);

        TreeSet<String> candSet = new TreeSet<>(candidates);
        List<String> candUserIds = new ArrayList<>();
        for (String uid : userIds) if (candSet.contains(uid)) candUserIds.add(uid);

        Matrices candMatrices = buildMetricMatrices(prepared, candUserIds, timeIndex);
        Baselines baselines = historicalBaselinesByOffset(cfg, historyPrepared, candUserIds, timeIndex, checkedNaive);

        int cu = candUserIds.size();
        int wlen = b - a + 1;

        // ---- 评分（固定 both 权重：rpm / input / output 三维全参与）----
        double[] rpmExcessSum = new double[cu], promptDeltaSum = new double[cu], complDeltaSum = new double[cu];
        double[][] rpmExcessW = new double[cu][wlen], promptDeltaW = new double[cu][wlen], complDeltaW = new double[cu][wlen];
        for (int u = 0; u < cu; u++) {
            for (int j = 0; j < wlen; j++) {
                int col = a + j;
                double re = Math.max(0.0, candMatrices.rpm[u][col] - baselines.rpm[u][col]);
                double pd = Math.max(0.0, candMatrices.prompt[u][col] - baselines.prompt[u][col]);
                double cd = Math.max(0.0, candMatrices.completion[u][col] - baselines.completion[u][col]);
                rpmExcessW[u][j] = re; promptDeltaW[u][j] = pd; complDeltaW[u][j] = cd;
                rpmExcessSum[u] += re; promptDeltaSum[u] += pd; complDeltaSum[u] += cd;
            }
        }

        double[] rpmRatio = safeRatio(rpmExcessSum);
        double[] promptRatio = safeRatio(promptDeltaSum);
        double[] complRatio = safeRatio(complDeltaSum);

        double wRpm = SCORE_WEIGHTS[0], wInput = SCORE_WEIGHTS[1], wOutput = SCORE_WEIGHTS[2];
        double[] scores = new double[cu];
        for (int u = 0; u < cu; u++) scores[u] = wRpm * rpmRatio[u] + wInput * promptRatio[u] + wOutput * complRatio[u];
        double scoreSum = sum(scores);
        double[] scoreRatio = new double[cu];
        if (scoreSum > 0) for (int u = 0; u < cu; u++) scoreRatio[u] = scores[u] / scoreSum;

        Integer[] order = new Integer[cu];
        for (int i = 0; i < cu; i++) order[i] = i;
        Arrays.sort(order, (x, y) -> Double.compare(scores[y], scores[x]));

        List<Map<String, Object>> culprits = new ArrayList<>();
        // 与 culprit 平行保存可执行杠杆信息，供 Round 3 使用
        List<Map<String, Object>> culpritLevers = new ArrayList<>();
        List<String> culpritProcess = new ArrayList<>();
        double cumulative = 0.0;
        for (int idx : order) {
            if (scores[idx] <= 0) break;
            double ratio = scoreRatio[idx];
            if (!culprits.isEmpty() && ratio < cfg.culpritMinRatio) break;

            double[] localScore = combinedLocalScore(rpmExcessW[idx], promptDeltaW[idx], complDeltaW[idx], SCORE_WEIGHTS);
            int peakOffset = localScore.length > 0 ? argmax(localScore) : 0;
            int peakIdx = a + peakOffset;

            Map<String, Double> currentScalars = new LinkedHashMap<>();
            currentScalars.put("rpm", windowMean(slice(candMatrices.rpm[idx], a, b)));
            currentScalars.put("tpm", windowMean(slice(candMatrices.tpm[idx], a, b)));
            currentScalars.put("prompt_tokens", windowMean(slice(candMatrices.prompt[idx], a, b)));
            currentScalars.put("completion_tokens", windowMean(slice(candMatrices.completion[idx], a, b)));

            Map<String, Double> baselineScalars = new LinkedHashMap<>();
            baselineScalars.put("rpm", baselineScalar(slice(baselines.rpm[idx], a, b)));
            baselineScalars.put("tpm", baselineScalar(slice(baselines.tpm[idx], a, b)));
            baselineScalars.put("prompt_tokens", baselineScalar(slice(baselines.prompt[idx], a, b)));
            baselineScalars.put("completion_tokens", baselineScalar(slice(baselines.completion[idx], a, b)));

            Classify cls = classifyScenario(cfg, currentScalars, baselineScalars);

            String uid = candUserIds.get(idx);
            Map<String, Object> culprit = new LinkedHashMap<>();
            culprit.put("domain_id", uid);
            culprit.put("score", scores[idx]);
            culprit.put("score_ratio", ratio);
            culprit.put("scenario", cls.scenario);
            culprit.put("peak_time", formatTs(timeIndex, peakIdx, tz));
            culprit.put("peak_rpm", candMatrices.rpm[idx][peakIdx]);
            culprit.put("peak_tpm", candMatrices.tpm[idx][peakIdx]);
            culprit.put("peak_ttft", candMatrices.ttft[idx][peakIdx]);
            culprit.put("peak_tpot", candMatrices.tpot[idx][peakIdx]);
            culprit.put("peak_prompt_tokens", candMatrices.prompt[idx][peakIdx]);
            culprit.put("peak_completion_tokens", candMatrices.completion[idx][peakIdx]);

            List<String> warnings = new ArrayList<>();
            if (!cls.suppressed.isEmpty()) warnings.add("baseline_unavailable: " + String.join(", ", cls.suppressed));

            Map<String, Object> leverForR3 = null;
            String processForR3 = null;
            if (cls.scenario == null) {
                culprit.put("note", "no_scenario_triggered");
            } else {
                LeverResult lr = computePoolLever(cfg, cls.scenario, currentScalars, baselineScalars, cls.ratios, cls.triggered);
                culprit.put("process_type", lr.processType);
                if (lr.lever != null) { culprit.put("pool_lever", lr.lever); leverForR3 = lr.lever; processForR3 = lr.processType; }
                if (lr.note != null) culprit.put("note", lr.note);
                if (lr.warning != null) warnings.add(lr.warning);
            }
            if (!warnings.isEmpty()) culprit.put("warning", String.join("; ", warnings));
            culprits.add(culprit);
            culpritLevers.add(leverForR3);
            culpritProcess.add(processForR3);
            cumulative += ratio;
            if (culprits.size() >= cfg.culpritTopK || cumulative >= cfg.culpritCumRatio) break;
        }

        // ---- Round 3: region/常驻服务拆分（仅对有杠杆的 culprits，一次查询）----
        List<Map<String, Object>> strategies = new ArrayList<>();
        List<Integer> leverIdx = new ArrayList<>();
        for (int i = 0; i < culprits.size(); i++) if (culpritLevers.get(i) != null) leverIdx.add(i);
        if (!leverIdx.isEmpty()) {
            long evStartMs = toEpochMs(timeIndex.get(a).atZone(tz));
            long evEndMs = toEpochMs(timeIndex.get(b).atZone(tz));
            List<Object> domainIn = new ArrayList<>();
            for (int i : leverIdx) domainIn.add(culprits.get(i).get("domain_id"));
            List<Object> r3Filters = new ArrayList<>();
            r3Filters.add(orderedMap("name", "domain_id", "operator", "in", "value", domainIn));
            r3Filters.add(orderedMap("name", "model_name", "operator", "=", "value", modelName));
            r3Filters.add(orderedMap("name", "timestamp", "operator", ">=", "value", String.valueOf(evStartMs)));
            r3Filters.add(orderedMap("name", "timestamp", "operator", "<=", "value", String.valueOf(evEndMs)));
            logf("[round3] query region breakdown culprits=%d", leverIdx.size());
            List<Map<String, Object>> r3Rows = client.query(r3Filters, r3Dimensions());
            List<Rec> r3Df = rowsToRecords(r3Rows, cfg.timezone, true);
            logf("[round3] rows=%d df_rows=%d", r3Rows.size(), r3Df.size());
            for (int i : leverIdx) {
                Map<String, Object> culprit = culprits.get(i);
                Breakdown bd = buildRegionBreakdown(r3Df, (String) culprit.get("domain_id"), serviceId, culpritLevers.get(i));
                if (bd.note != null) {
                    Object existing = culprit.get("note");
                    culprit.put("note", existing != null ? existing + "; " + bd.note : bd.note);
                    continue;
                }
                culprit.put("region_breakdown", bd.rows);
                String processType = culpritProcess.get(i);
                String scenarioType = (String) ((Map<?, ?>) culprit.get("scenario")).get("type");
                for (Map<String, Object> row : bd.rows) {
                    Map<String, Object> strat = new LinkedHashMap<>();
                    strat.put("domain_id", culprit.get("domain_id"));
                    strat.put("resident_model_id", row.get("resident_model_id"));
                    strat.put("region", row.get("region"));
                    strat.put("process_type", processType);
                    strat.put("value", row.get("value"));
                    strat.put("model_name", modelName);
                    strat.put("project_id", row.getOrDefault("project_id", ""));
                    strat.put("scenario", scenarioType);
                    strategies.add(strat);
                }
            }
        }

        base.put("culprits", culprits);
        base.put("strategies", strategies);
        base.put("api_call_count", client.httpCallCount);
        Map<String, Object> histBase = new LinkedHashMap<>();
        histBase.put("candidates", new ArrayList<Object>(candidates));
        histBase.put("history_rows", historyPrepared.size());
        histBase.put("history_days", cfg.historyDays);
        histBase.put("history_window_end", r2EndInclusive.format(ISO_MIN));
        base.put("history_baseline", histBase);
        if (culprits.isEmpty()) base.put("warning", "no_culprit_resolved_baseline_may_be_empty");
        return new Result(base, 0);
    }

    // ========================================================================
    // 入口
    // ========================================================================
    static final int EXPECTED_ARG_COUNT = 7;
    static final String[] ARG_NAMES = {"service_id", "model_name", "time", "maasApiurl", "appcode", "applydomainid", "applyprojectid"};

    public static void main(String[] argv) {
        System.exit(run(argv));
    }

    static int run(String[] argv) {
        if (argv.length != EXPECTED_ARG_COUNT) {
            Map<String, Object> err = new LinkedHashMap<>();
            err.put("status", "error");
            err.put("error_type", "InvalidArgs");
            err.put("error_msg", "expected " + EXPECTED_ARG_COUNT + " positional args ("
                    + String.join(", ", ARG_NAMES) + "), got " + argv.length);
            return emit(err, 1);
        }
        try {
            Result r = runPlugin(argv[0], argv[1], argv[2], argv[3], argv[4], argv[5], argv[6]);
            return emit(r.payload, r.exitCode);
        } catch (MaasApiError exc) {
            Map<String, Object> err = new LinkedHashMap<>();
            err.put("status", "error");
            err.put("error_type", "MaasApiError");
            err.put("error_msg", exc.getMessage());
            err.put("api_status", exc.status);
            return emit(err, 1);
        } catch (IllegalArgumentException exc) {
            Map<String, Object> err = new LinkedHashMap<>();
            err.put("status", "error");
            err.put("error_type", "ValueError");
            err.put("error_msg", exc.getMessage());
            return emit(err, 1);
        } catch (Exception exc) {
            Map<String, Object> err = new LinkedHashMap<>();
            err.put("status", "error");
            err.put("error_type", exc.getClass().getSimpleName());
            err.put("error_msg", String.valueOf(exc.getMessage()));
            return emit(err, 1);
        }
    }

    // ========================================================================
    // 极简 JSON 解析 / 序列化（无外部依赖，与 MaasPlugin.java 同款）
    // ========================================================================
    static final class Json {
        private final String s;
        private int i;

        private Json(String s) { this.s = s; }

        static Object parse(String s) {
            Json p = new Json(s);
            p.ws();
            Object v = p.value();
            p.ws();
            return v;
        }

        private void ws() { while (i < s.length() && Character.isWhitespace(s.charAt(i))) i++; }

        private Object value() {
            ws();
            char c = s.charAt(i);
            switch (c) {
                case '{': return obj();
                case '[': return arr();
                case '"': return str();
                case 't': i += 4; return Boolean.TRUE;
                case 'f': i += 5; return Boolean.FALSE;
                case 'n': i += 4; return null;
                default: return num();
            }
        }

        private Map<String, Object> obj() {
            Map<String, Object> m = new LinkedHashMap<>();
            i++; // {
            ws();
            if (s.charAt(i) == '}') { i++; return m; }
            while (true) {
                ws();
                String key = str();
                ws();
                i++; // :
                Object val = value();
                m.put(key, val);
                ws();
                char c = s.charAt(i++);
                if (c == ',') continue;
                if (c == '}') break;
                throw new RuntimeException("bad object at " + i);
            }
            return m;
        }

        private List<Object> arr() {
            List<Object> a = new ArrayList<>();
            i++; // [
            ws();
            if (s.charAt(i) == ']') { i++; return a; }
            while (true) {
                a.add(value());
                ws();
                char c = s.charAt(i++);
                if (c == ',') continue;
                if (c == ']') break;
                throw new RuntimeException("bad array at " + i);
            }
            return a;
        }

        private String str() {
            StringBuilder sb = new StringBuilder();
            i++; // opening quote
            while (true) {
                char c = s.charAt(i++);
                if (c == '"') break;
                if (c == '\\') {
                    char e = s.charAt(i++);
                    switch (e) {
                        case '"': sb.append('"'); break;
                        case '\\': sb.append('\\'); break;
                        case '/': sb.append('/'); break;
                        case 'b': sb.append('\b'); break;
                        case 'f': sb.append('\f'); break;
                        case 'n': sb.append('\n'); break;
                        case 'r': sb.append('\r'); break;
                        case 't': sb.append('\t'); break;
                        case 'u':
                            sb.append((char) Integer.parseInt(s.substring(i, i + 4), 16));
                            i += 4;
                            break;
                        default: sb.append(e);
                    }
                } else {
                    sb.append(c);
                }
            }
            return sb.toString();
        }

        private Object num() {
            int start = i;
            while (i < s.length()) {
                char c = s.charAt(i);
                if (c == '-' || c == '+' || c == '.' || c == 'e' || c == 'E' || (c >= '0' && c <= '9')) i++;
                else break;
            }
            return Double.parseDouble(s.substring(start, i));
        }

        // -------- writer --------
        static String write(Object o) {
            StringBuilder sb = new StringBuilder();
            writeValue(o, sb, "");
            return sb.toString();
        }

        @SuppressWarnings("unchecked")
        private static void writeValue(Object o, StringBuilder sb, String indent) {
            if (o == null) { sb.append("null"); return; }
            if (o instanceof Map) {
                Map<String, Object> m = (Map<String, Object>) o;
                if (m.isEmpty()) { sb.append("{}"); return; }
                String inner = indent + "  ";
                sb.append("{\n");
                int n = m.size(), k = 0;
                for (Map.Entry<String, Object> e : m.entrySet()) {
                    sb.append(inner);
                    writeString(e.getKey(), sb);
                    sb.append(": ");
                    writeValue(e.getValue(), sb, inner);
                    if (++k < n) sb.append(",");
                    sb.append("\n");
                }
                sb.append(indent).append("}");
            } else if (o instanceof List) {
                List<Object> a = (List<Object>) o;
                if (a.isEmpty()) { sb.append("[]"); return; }
                String inner = indent + "  ";
                sb.append("[\n");
                for (int k = 0; k < a.size(); k++) {
                    sb.append(inner);
                    writeValue(a.get(k), sb, inner);
                    if (k < a.size() - 1) sb.append(",");
                    sb.append("\n");
                }
                sb.append(indent).append("]");
            } else if (o instanceof String) {
                writeString((String) o, sb);
            } else if (o instanceof Boolean) {
                sb.append(o.toString());
            } else if (o instanceof Number) {
                sb.append(numStr((Number) o));
            } else {
                writeString(o.toString(), sb);
            }
        }

        private static String numStr(Number n) {
            if (n instanceof Double || n instanceof Float) {
                double d = n.doubleValue();
                if (!Double.isFinite(d)) return "0.0";
                return Double.toString(d);
            }
            return n.toString();
        }

        private static void writeString(String s, StringBuilder sb) {
            sb.append('"');
            for (int i = 0; i < s.length(); i++) {
                char c = s.charAt(i);
                switch (c) {
                    case '"': sb.append("\\\""); break;
                    case '\\': sb.append("\\\\"); break;
                    case '\n': sb.append("\\n"); break;
                    case '\r': sb.append("\\r"); break;
                    case '\t': sb.append("\\t"); break;
                    case '\b': sb.append("\\b"); break;
                    case '\f': sb.append("\\f"); break;
                    default:
                        if (c < 0x20) sb.append(String.format("\\u%04x", (int) c));
                        else sb.append(c);
                }
            }
            sb.append('"');
        }
    }
}
