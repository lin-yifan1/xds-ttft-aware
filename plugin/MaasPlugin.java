// MaaS 过载溯源插件 - 单文件 Java 版本 (java main.py 的等价实现)
//
// 入参（位置参数，顺序固定）：
//     1. domain_id          告警上报租户 ID
//     2. service_id         infer_service_id
//     3. time               ISO 8601 字符串或数字时间戳（秒/毫秒自动判定）
//     4. maasApiurl         MaaS 数据查询接口完整端点 URL
//     5. appcode            -> X-Apig-AppCode header
//     6. applydomainid      -> X-Apply-DomainID header
//     7. applyprojectid     -> X-Apply-ProjectID header
//
// 可选环境变量（覆盖默认）：
//     PLUGIN_TTFT_SLA / PLUGIN_TPOT_SLA / PLUGIN_SEVERE_RATIO /
//     PLUGIN_MILD_CONSECUTIVE_WINDOWS / PLUGIN_HISTORY_DAYS / PLUGIN_CANDIDATE_TOP_N /
//     PLUGIN_CULPRIT_TOP_K / PLUGIN_SCENARIO_TRIGGER_FACTOR / PLUGIN_TPM_CAP_FACTOR /
//     PLUGIN_OUTPUT_CAP_FACTOR / PLUGIN_RPM_SHRINK_FACTOR / PLUGIN_TIMEZONE
//
// 输出契约：stdout 一段多行 JSON；进度日志写 stderr。status 取值：
//     anomaly / normal / no_data / error (error 配合 exit code 1)
//
// 编译运行 (JDK 11+)：
//     javac MaasPlugin.java
//     java MaasPlugin <domain_id> <service_id> <time> <maasApiurl> <appcode> <applydomainid> <applyprojectid>

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
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;
import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;

public class MaasPlugin {

    static final double EPSILON = 1e-9;
    static final DateTimeFormatter ISO_MIN =
            DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mmxxx");

    // 三维评分权重 (w_rpm, w_input, w_output)。
    static double[] weightsForScope(String scope) {
        switch (scope) {
            case "ttft_only": return new double[]{0.4667, 0.5333, 0.0};
            case "tpot_only": return new double[]{0.0, 0.0, 1.0};
            default:          return new double[]{0.28125, 0.34375, 0.375}; // both
        }
    }

    // 每个 scope 下物理上允许点亮的场景（严格门控）。
    static String[] eligibleScenarios(String scope) {
        switch (scope) {
            case "ttft_only": return new String[]{"rpm_increase", "input_too_long"};
            case "tpot_only": return new String[]{"output_too_long"};
            default:          return new String[]{"rpm_increase", "input_too_long", "output_too_long"};
        }
    }

    // ========================================================================
    // 配置
    // ========================================================================
    static final class PluginConfig {
        double ttftSla = 15000.0;
        double tpotSla = 50.0;
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
        double tpmCapFactor = 1.5;
        double outputCapFactor = 1.5;
        double rpmShrinkFactor = 0.8;
        int windowBeforeMinutes = 30;
        int windowAfterMinutes = 30;
        int historySameTimeMinutes = 10;
        int pageSize = 2000;
        double timeoutSeconds = 30.0;
        String timezone = "Asia/Shanghai";

        static PluginConfig loadFromEnv() {
            PluginConfig c = new PluginConfig();
            c.ttftSla = envFloat("PLUGIN_TTFT_SLA", c.ttftSla);
            c.tpotSla = envFloat("PLUGIN_TPOT_SLA", c.tpotSla);
            c.severeRatio = envFloat("PLUGIN_SEVERE_RATIO", c.severeRatio);
            c.mildConsecutiveWindows = envInt("PLUGIN_MILD_CONSECUTIVE_WINDOWS", c.mildConsecutiveWindows);
            c.historyDays = envInt("PLUGIN_HISTORY_DAYS", c.historyDays);
            c.candidateTopN = envInt("PLUGIN_CANDIDATE_TOP_N", c.candidateTopN);
            c.culpritTopK = envInt("PLUGIN_CULPRIT_TOP_K", c.culpritTopK);
            c.scenarioTriggerFactor = envFloat("PLUGIN_SCENARIO_TRIGGER_FACTOR", c.scenarioTriggerFactor);
            c.tpmCapFactor = envFloat("PLUGIN_TPM_CAP_FACTOR", c.tpmCapFactor);
            c.outputCapFactor = envFloat("PLUGIN_OUTPUT_CAP_FACTOR", c.outputCapFactor);
            c.rpmShrinkFactor = envFloat("PLUGIN_RPM_SHRINK_FACTOR", c.rpmShrinkFactor);
            c.timezone = envStr("PLUGIN_TIMEZONE", c.timezone);
            return c;
        }

        Map<String, Object> echo() {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("ttft_sla", ttftSla);
            m.put("tpot_sla", tpotSla);
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
            m.put("tpm_cap_factor", tpmCapFactor);
            m.put("output_cap_factor", outputCapFactor);
            m.put("rpm_shrink_factor", rpmShrinkFactor);
            m.put("window_before_minutes", windowBeforeMinutes);
            m.put("window_after_minutes", windowAfterMinutes);
            m.put("history_same_time_minutes", historySameTimeMinutes);
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

    static int envInt(String name, int def) {
        String raw = System.getenv(name);
        if (raw == null || raw.isEmpty()) return def;
        try { return Integer.parseInt(raw.trim()); } catch (Exception e) { return def; }
    }

    static String envStr(String name, String def) {
        String raw = System.getenv(name);
        if (raw == null || raw.isEmpty()) return def;
        return raw;
    }

    // ========================================================================
    // 时间解析
    // ========================================================================
    static ZonedDateTime parseIsoReportedAt(String value, String defaultTz) {
        String text = value == null ? "" : value.trim();
        if (text.isEmpty()) throw new IllegalArgumentException("time argument is empty");
        ZoneId tz = ZoneId.of(defaultTz);
        // 优先尝试时间戳格式（纯数字，支持秒/毫秒）
        try {
            double ts = Double.parseDouble(text);
            if (ts > 1e12) ts = ts / 1000.0;
            ZonedDateTime parsed = Instant.ofEpochSecond((long) ts).atZone(tz);
            return parsed.withSecond(0).withNano(0);
        } catch (NumberFormatException ignore) {
            // fall through
        }
        // 回退到 ISO 8601 字符串
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
        final HttpClient client;
        int httpCallCount = 0;

        MaasClient(String url, String appcode, String applyDomainId, String applyProjectId,
                   double timeoutSeconds, int pageSize) {
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
            this.client = buildInsecureClient(timeoutSeconds);
        }

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> query(List<Object> filters) {
            List<Map<String, Object>> rows = new ArrayList<>();
            int pageNum = 1;
            while (true) {
                Map<String, Object> payload = new LinkedHashMap<>();
                payload.put("dimensions", queryDimensions());
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

    static List<Rec> rowsToRecords(List<Map<String, Object>> rows, String tzName) {
        ZoneId tz = ZoneId.of(tzName);
        List<Rec> out = new ArrayList<>();
        for (Map<String, Object> row : rows) {
            String domainId = String.valueOf(row.getOrDefault("domain_id", "")).trim();
            if (domainId.isEmpty() || domainId.equals("null")) continue;
            if (row.containsKey("infer_service_id")) {
                String sid = String.valueOf(row.get("infer_service_id") == null ? "" : row.get("infer_service_id")).trim();
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
        double ttft, tpot, prompt, completion;
    }

    static List<Rec> prepareFrame(List<Rec> df) {
        if (df.isEmpty()) return new ArrayList<>();
        Map<String, Agg> groups = new LinkedHashMap<>();
        for (Rec r : df) {
            String key = r.domainId + " " + r.time.toString();
            Agg a = groups.get(key);
            if (a == null) { a = new Agg(); a.domainId = r.domainId; a.time = r.time; groups.put(key, a); }
            double w = (Double.isFinite(r.rpm) && r.rpm > 0) ? r.rpm : 0.0;
            a.rpmSum += r.rpm;
            a.tpmSum += r.tpm;
            // ttft / tpot: 有效需 finite & !=0 & w>0
            if (Double.isFinite(r.ttft) && r.ttft != 0 && w > 0) { a.ttftW += w; a.ttftWv += r.ttft * w; }
            if (Double.isFinite(r.tpot) && r.tpot != 0 && w > 0) { a.tpotW += w; a.tpotWv += r.tpot * w; }
            // prompt / completion: 有效需 finite & w>0
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
    // 事件检测
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

    static List<int[]> capEvents(List<int[]> events, int maxEvents, double[] sysTtft, double[] sysTpot, PluginConfig cfg) {
        if (events.isEmpty() || maxEvents <= 0 || events.size() <= maxEvents) return events;
        List<double[]> scored = new ArrayList<>(); // [index, score]
        for (int i = 0; i < events.size(); i++) {
            int[] w = events.get(i);
            double ttftRatio = Double.NEGATIVE_INFINITY, tpotRatio = Double.NEGATIVE_INFINITY;
            for (int j = w[0]; j <= w[1]; j++) {
                ttftRatio = Math.max(ttftRatio, sysTtft[j] / Math.max(cfg.ttftSla, EPSILON));
                tpotRatio = Math.max(tpotRatio, sysTpot[j] / Math.max(cfg.tpotSla, EPSILON));
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

    static EventInfo detectSystemEvents(PluginConfig cfg, double[] sysTtft, double[] sysTpot) {
        int T = sysTtft.length;
        boolean[] ttftHeavy = new boolean[T], tpotHeavy = new boolean[T];
        boolean[] ttftMild = new boolean[T], tpotMild = new boolean[T];
        for (int i = 0; i < T; i++) {
            ttftHeavy[i] = sysTtft[i] >= cfg.ttftSla * cfg.severeRatio;
            tpotHeavy[i] = sysTpot[i] >= cfg.tpotSla * cfg.severeRatio;
            ttftMild[i] = sysTtft[i] > cfg.ttftSla;
            tpotMild[i] = sysTpot[i] > cfg.tpotSla;
        }
        boolean[] ttftRun = markRuns(ttftMild, cfg.mildConsecutiveWindows);
        boolean[] tpotRun = markRuns(tpotMild, cfg.mildConsecutiveWindows);
        EventInfo info = new EventInfo();
        info.sysAnomTtft = new boolean[T]; info.sysAnomTpot = new boolean[T]; info.sysAnom = new boolean[T];
        for (int i = 0; i < T; i++) {
            info.sysAnomTtft[i] = ttftHeavy[i] || ttftRun[i];
            info.sysAnomTpot[i] = tpotHeavy[i] || tpotRun[i];
            info.sysAnom[i] = info.sysAnomTtft[i] || info.sysAnomTpot[i];
        }
        List<int[]> events = maskToEvents(info.sysAnom, cfg.eventMergeGap);
        events = capEvents(events, cfg.maxEvents, sysTtft, sysTpot, cfg);
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

    // 场景定义：trigger 指标 / remediation 杠杆。
    static final class ScenarioSpec {
        String triggerMetric, action, targetMetric, factorAttr;
        ScenarioSpec(String t, String a, String tm, String fa) { triggerMetric = t; action = a; targetMetric = tm; factorAttr = fa; }
    }

    static ScenarioSpec scenarioSpec(String type) {
        switch (type) {
            case "rpm_increase":   return new ScenarioSpec("rpm", "throttle_rpm", "rpm", "rpm_shrink_factor");
            case "input_too_long": return new ScenarioSpec("prompt_tokens", "cap_tpm", "tpm", "tpm_cap_factor");
            case "output_too_long":return new ScenarioSpec("completion_tokens", "cap_output_length", "completion_tokens", "output_cap_factor");
            default: throw new IllegalArgumentException("unknown scenario " + type);
        }
    }

    static double factorByAttr(PluginConfig cfg, String attr) {
        switch (attr) {
            case "rpm_shrink_factor": return cfg.rpmShrinkFactor;
            case "tpm_cap_factor": return cfg.tpmCapFactor;
            case "output_cap_factor": return cfg.outputCapFactor;
            default: return 0;
        }
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

    static final class ScenarioResult {
        List<Map<String, Object>> fired = new ArrayList<>();
        List<String> suppressed = new ArrayList<>();
    }

    static ScenarioResult buildScenariosForCulprit(PluginConfig cfg, String scope,
                                                   Map<String, Double> current, Map<String, Double> baseline) {
        ScenarioResult res = new ScenarioResult();
        for (String type : eligibleScenarios(scope)) {
            ScenarioSpec spec = scenarioSpec(type);
            Double triggerBaseline = baseline.get(spec.triggerMetric);
            Double targetBaseline = baseline.get(spec.targetMetric);
            if (triggerBaseline == null || targetBaseline == null) { res.suppressed.add(type); continue; }
            double cur = current.getOrDefault(spec.triggerMetric, 0.0);
            if (cur < triggerBaseline * cfg.scenarioTriggerFactor) continue;
            double factor = factorByAttr(cfg, spec.factorAttr);
            Map<String, Object> trigger = new LinkedHashMap<>();
            trigger.put("metric", spec.triggerMetric);
            trigger.put("current", cur);
            trigger.put("baseline", triggerBaseline);
            trigger.put("ratio", cur / triggerBaseline);
            Map<String, Object> remediation = new LinkedHashMap<>();
            remediation.put("action", spec.action);
            remediation.put("target_metric", spec.targetMetric);
            remediation.put("baseline", targetBaseline);
            remediation.put("factor", factor);
            remediation.put("recommended_value", targetBaseline * factor);
            Map<String, Object> scenario = new LinkedHashMap<>();
            scenario.put("type", type);
            scenario.put("trigger", trigger);
            scenario.put("remediation", remediation);
            res.fired.add(scenario);
        }
        res.fired.sort((x, y) -> {
            double rx = (Double) ((Map<?, ?>) x.get("trigger")).get("ratio");
            double ry = (Double) ((Map<?, ?>) y.get("trigger")).get("ratio");
            return Double.compare(ry, rx);
        });
        return res;
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

        // group by (domain, offset): count + sums
        final class GAgg { int count; double rpm, tpm, prompt, completion; }
        Map<String, GAgg> groups = new LinkedHashMap<>();
        Map<String, String> groupDomain = new LinkedHashMap<>();
        Map<String, Integer> groupOffset = new LinkedHashMap<>();
        for (Rec r : history) {
            int off = offsetFromAnchor(r.time, reported);
            String key = r.domainId + " " + off;
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
    // 候选选择 + 系统统计
    // ========================================================================
    static List<String> pickCandidates(PluginConfig cfg, List<String> userIds, Matrices m, int a, int b, String alertReporter) {
        int U = userIds.size();
        double[] candScore = new double[U];
        for (int u = 0; u < U; u++) {
            double ttftMax = 0, tpotMax = 0;
            for (int j = a; j <= b; j++) { ttftMax = Math.max(ttftMax, m.ttft[u][j]); tpotMax = Math.max(tpotMax, m.tpot[u][j]); }
            candScore[u] = ttftMax / Math.max(cfg.ttftSla, EPSILON) + tpotMax / Math.max(cfg.tpotSla, EPSILON);
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
        if (alertReporter != null && !alertReporter.isEmpty() && !seen.contains(alertReporter)) chosen.add(alertReporter);
        return chosen;
    }

    static int indexForReportedAt(List<LocalDateTime> timeIndex, LocalDateTime reported) {
        if (timeIndex.isEmpty()) return -1;
        int best = 0; long bestDiff = Long.MAX_VALUE;
        for (int i = 0; i < timeIndex.size(); i++) {
            long diff = Math.abs(Duration.between(timeIndex.get(i), reported).getSeconds());
            if (diff < bestDiff) { bestDiff = diff; best = i; }
        }
        return best;
    }

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

    static Map<String, Object> systemStats(PluginConfig cfg, SystemSeries s, boolean[] sysAnom, int eventCount) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("hours", s.ttft.length);
        m.put("event_count", eventCount);
        int anomCount = 0;
        for (boolean v : sysAnom) if (v) anomCount++;
        m.put("system_anom_hours_count", anomCount);
        m.put("ttft", block(s.ttft, cfg.ttftSla, cfg));
        m.put("tpot", block(s.tpot, cfg.tpotSla, cfg));
        m.put("rpm", block(s.rpm, null, cfg));
        m.put("tpm", block(s.tpm, null, cfg));
        m.put("prompt_tokens", block(s.prompt, null, cfg));
        m.put("completion_tokens", block(s.completion, null, cfg));
        return m;
    }

    static String formatTs(List<LocalDateTime> timeIndex, int idx, ZoneId tz) {
        return timeIndex.get(idx).atZone(tz).format(ISO_MIN);
    }

    static List<Rec> splitHistory(List<Rec> df, LocalDateTime reported, int sameTimeMinutes) {
        if (df.isEmpty()) return new ArrayList<>();
        LocalDateTime cwStart = reported.minusMinutes(sameTimeMinutes);
        LocalDateTime cwEnd = reported.plusMinutes(sameTimeMinutes);
        List<Rec> out = new ArrayList<>();
        for (Rec r : df) {
            if (r.time.isBefore(cwStart) || !r.time.isBefore(cwEnd)) out.add(r);
        }
        return out;
    }

    // ========================================================================
    // 简单数学
    // ========================================================================
    static double sum(double[] a) { double s = 0; for (double v : a) s += v; return s; }
    static double mean(double[] a) { return a.length == 0 ? 0.0 : sum(a) / a.length; }
    static double max(double[] a) { double m = Double.NEGATIVE_INFINITY; for (double v : a) m = Math.max(m, v); return m; }

    static int argmaxRange(double[] a, int from, int to) {
        int best = 0; double bv = Double.NEGATIVE_INFINITY;
        for (int i = from; i <= to; i++) if (a[i] > bv) { bv = a[i]; best = i - from; }
        return best;
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

    // ========================================================================
    // 输出基础
    // ========================================================================
    static Map<String, Object> makeBaseOutput(String status, PluginConfig cfg, String domainId, String serviceId, ZonedDateTime reportedAt) {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("status", status);
        Map<String, Object> alert = new LinkedHashMap<>();
        alert.put("domain_id", domainId);
        alert.put("service_id", serviceId);
        alert.put("reported_at", reportedAt.format(ISO_MIN));
        out.put("alert", alert);
        out.put("config_echo", cfg.echo());
        return out;
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

    static Result runPlugin(String domainId, String serviceId, String timeIso, String maasUrl,
                            String appcode, String applyDomainId, String applyProjectId) {
        PluginConfig cfg = PluginConfig.loadFromEnv();
        ZoneId tz = ZoneId.of(cfg.timezone);
        ZonedDateTime reportedAt = parseIsoReportedAt(timeIso, cfg.timezone);
        LocalDateTime reportedNaive = reportedAt.toLocalDateTime();

        MaasClient client = new MaasClient(maasUrl, appcode, applyDomainId, applyProjectId,
                cfg.timeoutSeconds, cfg.pageSize);

        // ---- Round 1: service 当前 ±window 分 ----
        ZonedDateTime r1Start = reportedAt.minusMinutes(cfg.windowBeforeMinutes);
        ZonedDateTime r1End = reportedAt.plusMinutes(cfg.windowAfterMinutes);
        logf("[round1] query service=%s window=%s~%s", serviceId, r1Start.format(ISO_MIN), r1End.format(ISO_MIN));
        ZonedDateTime r1EndInclusive = r1End.minusMinutes(1);
        List<Object> r1Filters = new ArrayList<>();
        r1Filters.add(orderedMap("name", "infer_service_id", "operator", "=", "value", serviceId));
        r1Filters.add(orderedMap("name", "timestamp", "operator", ">=", "value", String.valueOf(toEpochMs(r1Start))));
        r1Filters.add(orderedMap("name", "timestamp", "operator", "<=", "value", String.valueOf(toEpochMs(r1EndInclusive))));
        List<Map<String, Object>> r1Rows = client.query(r1Filters);
        logf("[round1] rows=%d first_row=%s", r1Rows.size(), r1Rows.isEmpty() ? "null" : Json.write(r1Rows.get(0)));
        List<Rec> r1Df = rowsToRecords(r1Rows, cfg.timezone);
        logf("[round1] df_rows=%d after filter", r1Df.size());

        if (r1Df.isEmpty()) {
            Map<String, Object> out = makeBaseOutput("no_data", cfg, domainId, serviceId, reportedAt);
            out.put("api_call_count", client.httpCallCount);
            out.put("events", new ArrayList<>());
            out.put("culprits", new ArrayList<>());
            return new Result(out, 0);
        }

        List<Rec> prepared = prepareFrame(r1Df);
        TreeSet<String> uidSet = new TreeSet<>();
        for (Rec r : prepared) uidSet.add(r.domainId);
        List<String> userIds = new ArrayList<>(uidSet);
        int windowPoints = cfg.windowBeforeMinutes + cfg.windowAfterMinutes;
        LocalDateTime windowStartNaive = reportedNaive.minusMinutes(cfg.windowBeforeMinutes);
        List<LocalDateTime> timeIndex = new ArrayList<>();
        for (int i = 0; i < windowPoints; i++) timeIndex.add(windowStartNaive.plusMinutes(i));

        Matrices matrices = buildMetricMatrices(prepared, userIds, timeIndex);
        int U = userIds.size(), T = timeIndex.size();
        SystemSeries system = buildSystemSeries(matrices, T, U);
        EventInfo eventInfo = detectSystemEvents(cfg, system.ttft, system.tpot);
        List<int[]> events = eventInfo.events;

        int reportedIdx = indexForReportedAt(timeIndex, reportedNaive);
        int[] matchingEvent = null;
        for (int[] ev : events) {
            if (ev[0] <= reportedIdx && reportedIdx <= ev[1]) { matchingEvent = ev; break; }
        }

        Map<String, Object> base = makeBaseOutput("normal", cfg, domainId, serviceId, reportedAt);
        base.put("system_stats", systemStats(cfg, system, eventInfo.sysAnom, events.size()));

        if (matchingEvent == null) {
            base.put("events", new ArrayList<>());
            base.put("culprits", new ArrayList<>());
            base.put("api_call_count", client.httpCallCount);
            return new Result(base, 0);
        }

        int a = matchingEvent[0], b = matchingEvent[1];
        String scope = scopeForWindow(eventInfo.sysAnomTtft, eventInfo.sysAnomTpot, a, b);

        // ---- Round 1.5: 候选选择 ----
        List<String> candidates = pickCandidates(cfg, userIds, matrices, a, b, domainId);
        logf("[round1] event scope=%s candidates=%d uids=%s", scope, candidates.size(), candidates);

        // ---- Round 2: 跨池 14d × ±same_time 分 ----
        ZonedDateTime r2Start = reportedAt.minusDays(cfg.historyDays).minusMinutes(cfg.historySameTimeMinutes);
        ZonedDateTime r2End = reportedAt.plusMinutes(cfg.historySameTimeMinutes);
        logf("[round2] query history candidates=%d window=%s~%s", candidates.size(), r2Start.format(ISO_MIN), r2End.format(ISO_MIN));
        ZonedDateTime r2EndInclusive = r2End.minusMinutes(1);
        List<Object> r2Filters = new ArrayList<>();
        r2Filters.add(orderedMap("name", "domain_id", "operator", "in", "value", new ArrayList<Object>(candidates)));
        r2Filters.add(orderedMap("name", "timestamp", "operator", ">=", "value", String.valueOf(toEpochMs(r2Start))));
        r2Filters.add(orderedMap("name", "timestamp", "operator", "<=", "value", String.valueOf(toEpochMs(r2EndInclusive))));
        List<Map<String, Object>> r2Rows = client.query(r2Filters);
        List<Rec> r2Df = rowsToRecords(r2Rows, cfg.timezone);
        List<Rec> r2History = splitHistory(r2Df, reportedNaive, cfg.historySameTimeMinutes);
        List<Rec> historyPrepared = r2History.isEmpty() ? r2History : prepareFrame(r2History);

        // 候选 user_ids（顺序与 userIds sorted 一致），补上报者全 0 行
        TreeSet<String> candSet = new TreeSet<>(candidates);
        List<String> candUserIds = new ArrayList<>();
        for (String uid : userIds) if (candSet.contains(uid)) candUserIds.add(uid);
        for (String uid : candidates) if (!candUserIds.contains(uid)) candUserIds.add(uid);

        Matrices candMatrices = buildMetricMatrices(prepared, candUserIds, timeIndex);
        Baselines baselines = historicalBaselinesByOffset(cfg, historyPrepared, candUserIds, timeIndex, reportedNaive);

        int cu = candUserIds.size();
        int wlen = b - a + 1;

        // ---- 评分（三维：rpm / input / output）----
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

        double[] weights = weightsForScope(scope);
        double wRpm = weights[0], wInput = weights[1], wOutput = weights[2];
        double[] scores = new double[cu];
        for (int u = 0; u < cu; u++) scores[u] = wRpm * rpmRatio[u] + wInput * promptRatio[u] + wOutput * complRatio[u];
        double scoreSum = sum(scores);
        double[] scoreRatio = new double[cu];
        if (scoreSum > 0) for (int u = 0; u < cu; u++) scoreRatio[u] = scores[u] / scoreSum;

        Integer[] order = new Integer[cu];
        for (int i = 0; i < cu; i++) order[i] = i;
        Arrays.sort(order, (x, y) -> Double.compare(scores[y], scores[x]));

        List<Map<String, Object>> culprits = new ArrayList<>();
        double cumulative = 0.0;
        for (int idx : order) {
            if (scores[idx] <= 0) break;
            double ratio = scoreRatio[idx];
            if (!culprits.isEmpty() && ratio < cfg.culpritMinRatio) break;

            double[] localScore = combinedLocalScore(rpmExcessW[idx], promptDeltaW[idx], complDeltaW[idx], weights);
            int peakOffset = localScore.length > 0 ? argmax(localScore) : 0;
            int peakIdx = a + peakOffset;

            Map<String, Double> currentScalars = new LinkedHashMap<>();
            currentScalars.put("rpm", windowMean(slice(candMatrices.rpm[idx], a, b)));
            currentScalars.put("prompt_tokens", windowMean(slice(candMatrices.prompt[idx], a, b)));
            currentScalars.put("completion_tokens", windowMean(slice(candMatrices.completion[idx], a, b)));

            Map<String, Double> baselineScalars = new LinkedHashMap<>();
            baselineScalars.put("rpm", baselineScalar(slice(baselines.rpm[idx], a, b)));
            baselineScalars.put("tpm", baselineScalar(slice(baselines.tpm[idx], a, b)));
            baselineScalars.put("prompt_tokens", baselineScalar(slice(baselines.prompt[idx], a, b)));
            baselineScalars.put("completion_tokens", baselineScalar(slice(baselines.completion[idx], a, b)));

            ScenarioResult sr = buildScenariosForCulprit(cfg, scope, currentScalars, baselineScalars);

            String uid = candUserIds.get(idx);
            Map<String, Object> culprit = new LinkedHashMap<>();
            culprit.put("domain_id", uid);
            culprit.put("is_alert_reporter", uid.equals(domainId));
            culprit.put("score", scores[idx]);
            culprit.put("score_ratio", ratio);
            culprit.put("scenarios", sr.fired);
            culprit.put("peak_time", formatTs(timeIndex, peakIdx, tz));
            culprit.put("peak_rpm", candMatrices.rpm[idx][peakIdx]);
            culprit.put("peak_tpm", candMatrices.tpm[idx][peakIdx]);
            culprit.put("peak_ttft", candMatrices.ttft[idx][peakIdx]);
            culprit.put("peak_tpot", candMatrices.tpot[idx][peakIdx]);
            culprit.put("peak_prompt_tokens", candMatrices.prompt[idx][peakIdx]);
            culprit.put("peak_completion_tokens", candMatrices.completion[idx][peakIdx]);
            if (!sr.suppressed.isEmpty()) culprit.put("warning", "baseline_unavailable: " + String.join(", ", sr.suppressed));
            if (sr.fired.isEmpty()) culprit.put("note", "no_scenario_triggered");
            culprits.add(culprit);
            cumulative += ratio;
            if (culprits.size() >= cfg.culpritTopK || cumulative >= cfg.culpritCumRatio) break;
        }

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
        base.put("culprits", culprits);
        base.put("api_call_count", client.httpCallCount);
        Map<String, Object> histBase = new LinkedHashMap<>();
        histBase.put("candidates", new ArrayList<Object>(candidates));
        histBase.put("history_rows", historyPrepared.size());
        histBase.put("history_days", cfg.historyDays);
        histBase.put("history_same_time_minutes", cfg.historySameTimeMinutes);
        base.put("history_baseline", histBase);
        if (culprits.isEmpty()) base.put("warning", "no_culprit_resolved_baseline_may_be_empty");
        return new Result(base, 0);
    }

    static double[] slice(double[] a, int from, int to) {
        return Arrays.copyOfRange(a, from, to + 1);
    }

    static int argmax(double[] a) {
        int best = 0; double bv = Double.NEGATIVE_INFINITY;
        for (int i = 0; i < a.length; i++) if (a[i] > bv) { bv = a[i]; best = i; }
        return best;
    }

    // ========================================================================
    // 入口
    // ========================================================================
    static final int EXPECTED_ARG_COUNT = 7;
    static final String[] ARG_NAMES = {"domain_id", "service_id", "time", "maasApiurl", "appcode", "applydomainid", "applyprojectid"};

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
    // 极简 JSON 解析 / 序列化（无外部依赖）
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
