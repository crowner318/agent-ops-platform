package local.agentstorage;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
public class StorageController {
    private final JobRepository jobs;
    private final CaseRepository cases;
    private final ReportStore reports;
    public StorageController(JobRepository jobs,CaseRepository cases,ReportStore reports) {this.jobs=jobs;this.cases=cases;this.reports=reports;}
    @GetMapping("/health")
    public ResponseEntity<?> health() {
        boolean sql; try { sql=jobs.healthy(); } catch (RuntimeException e) { sql=false; }
        boolean redis=reports.healthy();
        return ResponseEntity.status(sql && redis ? 200 : 503).body(Map.of("service","agent-storage-java",
            "sqlite",Map.of("available",sql,"owner","java","engine","sqlite"),
            "redis",Map.of("available",redis,"owner","java","durability","depends_on_server_config"),
            "reports_backend","redis","status",sql && redis ? "ok" : "degraded"));
    }
    @GetMapping("/v1/jobs")
    public Map<String,Object> jobs(@RequestParam(defaultValue="false") boolean include_deleted) { return Map.of("jobs",jobs.list(include_deleted)); }
    @GetMapping("/v1/jobs/{id}")
    public Map<String,Object> job(@PathVariable String id) { return Map.of("job",jobs.get(id)); }
    @PostMapping("/v1/jobs")
    public ResponseEntity<?> createJob(@RequestBody Map<String,Object> input) { Values.only(input,Set.of("job")); return ResponseEntity.status(201).body(Map.of("job",jobs.create(object(input.get("job"))))); }
    @PatchMapping("/v1/jobs/{id}")
    public Map<String,Object> patchJob(@PathVariable String id,@RequestBody Map<String,Object> input) { Values.only(input,Set.of("values")); return Map.of("job",jobs.patch(id,object(input.get("values")))); }
    @PostMapping("/v1/jobs/recover")
    public Map<String,Object> recover(@RequestBody Map<String,Object> input) { Values.only(input,Set.of("error","updated_at")); return Map.of("recovered",jobs.recover(Values.text(input.get("error"),8192,false),Values.text(input.get("updated_at"),100,false))); }
    @PostMapping("/v1/jobs/import-tombstone")
    public Map<String,Object> importTombstone(@RequestBody Map<String,Object> input) { Values.only(input,Set.of("job")); return Map.of("job",jobs.importTombstone(object(input.get("job")))); }
    @PostMapping("/v1/jobs/{id}/purge")
    public Map<String,Object> purge(@PathVariable String id,@RequestBody Map<String,Object> input) { Values.confirmation(input,id); return Map.of("job",jobs.purge(id,reports)); }
    @PostMapping("/v1/reports/{id}")
    public ResponseEntity<?> putReport(@PathVariable String id,@RequestBody Map<String,Object> input) {
        jobs.storeReport(id,input,reports); return ResponseEntity.ok(Map.of("stored",true,"backend","redis"));
    }
    @GetMapping("/v1/reports/{id}")
    public Map<String,Object> report(@PathVariable String id) {
        if (jobs.get(id).get("purged_at") != null) throw new ApiException(404,"报告已永久删除。");
        return reports.get(id);
    }
    @DeleteMapping("/v1/reports/{id}")
    public Map<String,Object> deleteReport(@PathVariable String id) { reports.delete(id); return Map.of("deleted",true); }
    @GetMapping("/v1/cases")
    public Map<String,Object> cases(@RequestParam(required=false) String service,@RequestParam(required=false) String environment) { return Map.of("cases",cases.list(service,environment)); }
    @PostMapping("/v1/cases")
    public Map<String,Object> saveCase(@RequestBody Map<String,Object> input) { Values.only(input,Set.of("case")); return Map.of("case",cases.save(object(input.get("case")))); }
    @PostMapping("/v1/cases/import-tombstone")
    public Map<String,Object> importCaseTombstone(@RequestBody Map<String,Object> input) {
        Values.only(input,Set.of("id")); String id=Values.id(input.get("id"));
        cases.importTombstone(id); return Map.of("imported",true,"id",id);
    }
    @DeleteMapping("/v1/cases/{id}")
    public Map<String,Object> deleteCase(@PathVariable String id,@RequestBody Map<String,Object> input) { Values.confirmation(input,id); cases.delete(id); return Map.of("deleted",true); }
    @GetMapping("/v1/case-tombstones")
    public Map<String,Object> tombstones() { return Map.of("ids",cases.tombstones()); }
    @SuppressWarnings("unchecked")
    private Map<String,Object> object(Object value) {
        if (!(value instanceof Map<?,?> map) || map.keySet().stream().anyMatch(key -> !(key instanceof String))) throw new ApiException(400,"必要对象缺失或格式无效。");
        return (Map<String,Object>)value;
    }
}
