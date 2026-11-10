package local.agentstorage;

import java.util.Map;
import java.util.Set;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/v1/dispatch")
public class DispatchController {
    private final DispatchRepository dispatch;
    private final JobRepository jobs;

    public DispatchController(DispatchRepository dispatch, JobRepository jobs) {
        this.dispatch = dispatch;
        this.jobs = jobs;
    }

    @PostMapping
    public Map<String,Object> enqueue(@RequestBody Map<String,Object> input) {
        Values.only(input, Set.of("id", "task_id", "action", "job", "updated_at"));
        String id = Values.id(input.get("id"));
        String taskId = Values.id(input.get("task_id"));
        var action = Values.object(input.get("action"));
        var job = input.containsKey("job") ? Values.object(input.get("job")) : null;
        return Map.of("job", dispatch.enqueue(id, taskId, action, job, timestamp(input, "updated_at")));
    }

    @GetMapping("/pending")
    public Map<String,Object> pending() { return Map.of("events", dispatch.pending()); }

    @PostMapping("/{id}/published")
    public Map<String,Object> published(@PathVariable String id, @RequestBody Map<String,Object> input) {
        Values.only(input, Set.of());
        dispatch.published(id);
        return Map.of("published", true);
    }

    @PostMapping("/{id}/claim")
    public Map<String,Object> claim(@PathVariable String id, @RequestBody Map<String,Object> input) {
        Values.only(input, Set.of("worker_id", "updated_at"));
        return dispatch.claim(id, Values.id(input.get("worker_id")), timestamp(input, "updated_at"));
    }

    @PostMapping("/{id}/finish")
    public Map<String,Object> finish(@PathVariable String id, @RequestBody Map<String,Object> input) {
        Values.only(input, Set.of("worker_id", "updated_at"));
        dispatch.finish(id, Values.id(input.get("worker_id")), timestamp(input, "updated_at"));
        return Map.of("finished", true);
    }

    @PostMapping("/recover")
    public Map<String,Object> recover(@RequestBody Map<String,Object> input) {
        Values.only(input, Set.of("before", "updated_at"));
        return Map.of("recovered", dispatch.recover(timestamp(input, "before"), timestamp(input, "updated_at")));
    }

    @PostMapping("/recover-untracked")
    public Map<String,Object> recoverUntracked(@RequestBody Map<String,Object> input) {
        Values.only(input, Set.of("error", "updated_at"));
        return Map.of("recovered", jobs.recover(Values.text(input.get("error"), 8192, false), timestamp(input, "updated_at")));
    }

    private static String timestamp(Map<String,Object> input, String key) {
        return Values.text(input.get(key), 100, false);
    }
}
