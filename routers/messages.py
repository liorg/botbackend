using WhatsAppDockerManager.Configuration;
using WhatsAppDockerManager.Models;
using DbHost = WhatsAppDockerManager.Models.Host;
using Supabase;
namespace WhatsAppDockerManager.Services;

public interface IContainerManager
{
    Task InitializeAsync();
    Task<bool> StartPhoneContainerAsync(Phone phone);
    Task<bool> StopPhoneContainerAsync(Phone phone);
    Task<bool> RestartPhoneContainerAsync(Phone phone);
    Task SyncContainersAsync();
    Task HealthCheckAllAsync();
    Task TakeOverFromDeadHostAsync(Guid deadHostId);
    Guid? CurrentHostId { get; }
    Task<bool> PausePhoneContainerAsync(Phone phone);
    string? CurrentImageDigest { get; }  // ← חדש: גרסת ה-image הנוכחית
}

public class ContainerManager : IContainerManager
{
    private readonly IDockerService _dockerService;
    private readonly ISupabaseService _supabaseService;
    private readonly IConfiguration _configuration;
    private readonly ILogger<ContainerManager> _logger;
    private readonly HostSettings _hostSettings;
    private readonly DockerSettings _dockerSettings;
    
    private DbHost? _currentHost;
    private bool _initialized;
    private readonly SemaphoreSlim _initLock = new(1, 1);
    private readonly SemaphoreSlim _syncLock = new(1, 1);

    // ── Image info — מתאכלס אחרי pull ──────────────────────────────
    public Guid?   CurrentHostId      => _currentHost?.Id;
    public string? CurrentImageDigest { get; private set; }
    private DateTime? _currentImageCreated;

    public ContainerManager(
        IDockerService dockerService,
        ISupabaseService supabaseService,
        IConfiguration configuration,
        ILogger<ContainerManager> logger)
    {
        _dockerService   = dockerService;
        _supabaseService = supabaseService;
        _configuration   = configuration;
        _logger          = logger;
        _hostSettings    = configuration.GetSection("AppSettings:Host").Get<HostSettings>() ?? new();
        _dockerSettings  = configuration.GetSection("AppSettings:Docker").Get<DockerSettings>() ?? new();
    }

    public async Task InitializeAsync()
    {
        await _initLock.WaitAsync();
        try
        {
            if (_initialized) return;

            _logger.LogInformation("Initializing Container Manager...");

            // ── זיהוי HostName אוטומטי ───────────────────────────
            var hostName = _hostSettings.HostName;
            if (string.IsNullOrEmpty(hostName))
            {
                hostName = System.Net.Dns.GetHostName();
                _logger.LogInformation("Detected host name: {HostName}", hostName);
            }

            // ── זיהוי IP מקומי אוטומטי ──────────────────────────
            var localIp = _hostSettings.IpAddress;
            if (string.IsNullOrEmpty(localIp) || localIp == "0.0.0.0")
            {
                try
                {
                    localIp = System.Net.Dns.GetHostEntry(System.Net.Dns.GetHostName())
                        .AddressList
                        .FirstOrDefault(a => a.AddressFamily == System.Net.Sockets.AddressFamily.InterNetwork)
                        ?.ToString() ?? "0.0.0.0";
                    _logger.LogInformation("Detected local IP: {LocalIp}", localIp);
                }
                catch (Exception ex)
                {
                    _logger.LogWarning(ex, "Could not detect local IP");
                }
            }

            // ── זיהוי IP חיצוני אוטומטי ─────────────────────────
            var externalIp = _hostSettings.ExternalIp;
            if (string.IsNullOrEmpty(externalIp))
            {
                try
                {
                    using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(5) };
                    externalIp = (await http.GetStringAsync("http://checkip.amazonaws.com")).Trim();
                    _logger.LogInformation("Detected external IP: {ExternalIp}", externalIp);
                }
                catch (Exception ex)
                {
                    _logger.LogWarning(ex, "Could not detect external IP — using local IP as fallback");
                    externalIp = localIp;
                }
            }

            _currentHost = await _supabaseService.GetOrCreateHostAsync(
                hostName, localIp, externalIp,
                _hostSettings.PortRangeStart, _hostSettings.PortRangeEnd, _hostSettings.MaxContainers);

            if (_currentHost == null)
                throw new InvalidOperationException("Failed to register host in database");

            _logger.LogInformation("Host registered: {HostId} ({HostName})", _currentHost.Id, _currentHost.HostName);

            await _dockerService.EnsureNetworkExistsAsync("whatsapp_network");
            await _dockerService.EnsureRedisContainerRunningAsync();

            // ════════════════════════════════════════════════════════
            // PULL — תמיד מביא את הגרסה העדכנית ביותר
            // ════════════════════════════════════════════════════════
            _logger.LogInformation("🔄 Pulling latest image: {Image}", _dockerSettings.ImageName);
            try
            {
                var pullSuccess = await _dockerService.PullImageAsync(_dockerSettings.ImageName);
                if (pullSuccess)
                    _logger.LogInformation("✅ Image pulled successfully: {Image}", _dockerSettings.ImageName);
                else
                    _logger.LogWarning("⚠️ Pull returned false for {Image} — using cached version", _dockerSettings.ImageName);
            }
            catch (Exception ex)
            {
                // pull נכשל — לא עוצרים, ממשיכים עם image קיים
                _logger.LogWarning(ex, "⚠️ Pull failed for {Image} — continuing with cached version", _dockerSettings.ImageName);
            }

            // ── שמור image info אחרי pull ─────────────────────────
            try
            {
                var imageInfo = await _dockerService.GetImageInfoAsync(_dockerSettings.ImageName);
                if (imageInfo != null)
                {
                    CurrentImageDigest    = imageInfo.Id;
                    _currentImageCreated  = imageInfo.Created;
                    _logger.LogInformation(
                        "📦 Image version: {ShortDigest} | created: {Created}",
                        imageInfo.Id?[..Math.Min(20, imageInfo.Id?.Length ?? 0)],
                        imageInfo.Created.ToString("yyyy-MM-dd HH:mm:ss UTC"));
                }
            }
            catch (Exception ex)
            {
                _logger.LogWarning(ex, "Could not retrieve image info for {Image}", _dockerSettings.ImageName);
            }
            // ════════════════════════════════════════════════════════

            await SyncContainersAsync();

            _initialized = true;
            _logger.LogInformation("Container Manager initialized successfully");
        }
        finally
        {
            _initLock.Release();
        }
    }

    public async Task<bool> StartPhoneContainerAsync(Phone phone)
    {
        if (_currentHost == null)
        {
            _logger.LogError("Host not initialized");
            return false;
        }

        try
        {
            _logger.LogInformation("Starting container for phone {PhoneNumber}", phone.Number);

            if (phone.HostId == null)
            {
                _logger.LogInformation("Assigning phone {PhoneNumber} to host {HostId}", phone.Number, _currentHost.Id);
                await _supabaseService.AssignPhoneToHostAsync(phone.Id, _currentHost.Id);
                phone.HostId = _currentHost.Id;
            }

            await _supabaseService.UpdatePhoneDockerStatusAsync(phone.Id, PhoneDockerStatus.Starting);

            var (fastApiPort, baileysPort) = PortHashCalculator.GetBothPorts(phone.Number, _configuration);

            if (!string.IsNullOrEmpty(phone.CredsBase64))
                await RestoreCredsAsync(phone);

            var containerId = await _dockerService.CreateAndStartContainerAsync(phone);

            if (containerId == null)
            {
                await _supabaseService.UpdatePhoneDockerStatusAsync(phone.Id, PhoneDockerStatus.Error, errorMessage: "Failed to create container");
                await _supabaseService.LogAgentEventAsync(_currentHost.Id, AgentEventType.Error, new { phoneId = phone.Id, error = "Failed to create container" });
                return false;
            }

            var host = !string.IsNullOrEmpty(_hostSettings.ExternalIp) ? _hostSettings.ExternalIp
                     : !string.IsNullOrEmpty(_hostSettings.IpAddress)  ? _hostSettings.IpAddress
                     : "localhost";
            var dockerUrl = $"http://{host}:{fastApiPort}";

            await _supabaseService.UpdatePhoneDockerStatusAsync(
                phone.Id, PhoneDockerStatus.Running,
                containerId:   containerId,
                containerName: $"whatsapp_{phone.Number.Replace("+", "")}",
                apiPort:       fastApiPort,
                dockerUrl:     dockerUrl);

            await RegisterWebhookInContainerAsync(fastApiPort, phone.Id);
            await ReSendAuthIfConnectedAsync(fastApiPort, phone.Id);

            await _supabaseService.LogAgentEventAsync(_currentHost.Id, AgentEventType.Started,
                new { phoneId = phone.Id, containerId, fastApiPort, baileysPort, dockerUrl });

            _logger.LogInformation("Container started for phone {PhoneNumber} FastAPI:{FastApi} Baileys:{Baileys}",
                phone.Number, fastApiPort, baileysPort);
            return true;
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Error starting container for phone {PhoneNumber}", phone.Number);
            await _supabaseService.UpdatePhoneDockerStatusAsync(phone.Id, PhoneDockerStatus.Error, errorMessage: ex.Message);
            return false;
        }
    }

    private async Task RestoreCredsAsync(Phone phone)
    {
        try
        {
            var phoneIndex = phone.Number.Replace("+", "");
            var authPath   = Path.Combine(_dockerSettings.DataBasePath, $"auth_{phoneIndex}");
            Directory.CreateDirectory(authPath);
            var credsBytes = Convert.FromBase64String(phone.CredsBase64!);
            var credsPath  = Path.Combine(authPath, "creds.json");
            await File.WriteAllBytesAsync(credsPath, credsBytes);
            _logger.LogInformation("Restored creds.json for phone {PhoneNumber} → {Path}", phone.Number, credsPath);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Failed to restore creds for phone {PhoneNumber}", phone.Number);
        }
    }

    private async Task RegisterWebhookInContainerAsync(int fastApiPort, Guid phoneId)
    {
        try
        {
            await Task.Delay(8000);
            using var httpClient = new HttpClient { Timeout = TimeSpan.FromSeconds(15) };
            var host           = "172.17.0.1";
            var managerWebhook = $"http://{host}:5000/api/webhook/container-event/{phoneId}";
            var payload        = new { url = managerWebhook, secret = "manager-secret" };

            try
            {
                var listResponse = await httpClient.GetAsync($"http://localhost:{fastApiPort}/webhooks");
                if (listResponse.IsSuccessStatusCode)
                {
                    var listJson = await listResponse.Content.ReadFromJsonAsync<WebhookListResponse>();
                    if (listJson?.Webhooks != null)
                    {
                        foreach (var wh in listJson.Webhooks)
                        {
                            if (wh.Contains("container-event"))
                            {
                                try
                                {
                                    var delReq = new HttpRequestMessage(HttpMethod.Delete, $"http://localhost:{fastApiPort}/webhooks/unregister");
                                    delReq.Content = JsonContent.Create(new { url = wh });
                                    await httpClient.SendAsync(delReq);
                                    _logger.LogInformation("Unregistered stale webhook: {Url}", wh);
                                }
                                catch { }
                            }
                        }
                    }
                }
            }
            catch (Exception ex) { _logger.LogWarning("Could not clean webhooks: {Msg}", ex.Message); }

            for (int attempt = 1; attempt <= 3; attempt++)
            {
                try
                {
                    var response = await httpClient.PostAsJsonAsync($"http://localhost:{fastApiPort}/webhooks/register", payload);
                    if (response.IsSuccessStatusCode)
                    {
                        _logger.LogInformation("Webhook registered for phone {PhoneId} port {Port}", phoneId, fastApiPort);
                        return;
                    }
                }
                catch (Exception ex) { _logger.LogWarning("Webhook registration attempt {Attempt} error: {Message}", attempt, ex.Message); }
                if (attempt < 3) await Task.Delay(5000);
            }
        }
        catch (Exception ex) { _logger.LogWarning(ex, "Could not register webhook for phone {PhoneId}", phoneId); }
    }

    private async Task ReSendAuthIfConnectedAsync(int baileysPort, Guid phoneId)
    {
        try
        {
            using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(5) };
            var res = await http.GetFromJsonAsync<ContainerStatusResponse>($"http://localhost:{baileysPort}/status");
            if (res?.Status == "connected")
            {
                _logger.LogInformation("Container already connected, requesting creds resend for {PhoneId}", phoneId);
                await http.PostAsync($"http://localhost:{baileysPort}/resend-auth", null);
            }
        }
        catch (Exception ex) { _logger.LogWarning(ex, "Could not resend auth for phone {PhoneId}", phoneId); }
    }

    public async Task<bool> StopPhoneContainerAsync(Phone phone)
    {
        if (string.IsNullOrEmpty(phone.ContainerId)) { _logger.LogWarning("Phone {PhoneNumber} has no container ID", phone.Number); return false; }
        try
        {
            var success = await _dockerService.StopContainerAsync(phone.ContainerId);
            if (success)
            {
                await _supabaseService.UpdatePhoneDockerStatusAsync(phone.Id, PhoneDockerStatus.Stopped);
                await _supabaseService.LogAgentEventAsync(_currentHost?.Id, AgentEventType.Stopped, new { phoneId = phone.Id });
            }
            return success;
        }
        catch (Exception ex) { _logger.LogError(ex, "Error stopping container for phone {PhoneNumber}", phone.Number); return false; }
    }

    public async Task<bool> RestartPhoneContainerAsync(Phone phone)
    {
        await StopPhoneContainerAsync(phone);
        if (!string.IsNullOrEmpty(phone.ContainerId))
            await _dockerService.RemoveContainerAsync(phone.ContainerId);
        return await StartPhoneContainerAsync(phone);
    }

    public async Task SyncContainersAsync()
    {
        if (_currentHost == null) return;
        await _syncLock.WaitAsync();
        try
        {
            _logger.LogInformation("Syncing containers with database...");
            var phones = await _supabaseService.GetPhonesForHostAsync(_currentHost.Id);
            var runningContainers   = await _dockerService.ListContainersAsync(all: true);
            var runningContainerIds = runningContainers.Where(c => c.State == "running").Select(c => c.ID).ToHashSet();

            foreach (var phone in phones)
            {
                if (phone.DockerStatus == PhoneDockerStatus.Running && !string.IsNullOrEmpty(phone.ContainerId) && !runningContainerIds.Contains(phone.ContainerId))
                {
                    _logger.LogWarning("Container for phone {PhoneNumber} is not running, restarting...", phone.Number);
                    await RestartPhoneContainerAsync(phone);
                }
                else if (phone.DockerStatus == PhoneDockerStatus.Pending || phone.DockerStatus == PhoneDockerStatus.Unknown)
                {
                    _logger.LogInformation("Starting pending phone {PhoneNumber}", phone.Number);
                    await StartPhoneContainerAsync(phone);
                }
            }

            var orphanedPhones = await _supabaseService.GetOrphanedPhonesAsync();
            var currentCount   = phones.Count;
            foreach (var phone in orphanedPhones)
            {
                if (currentCount >= _hostSettings.MaxContainers) { _logger.LogWarning("Host at capacity ({Max}), cannot claim more phones", _hostSettings.MaxContainers); break; }
                _logger.LogInformation("Claiming orphaned phone {PhoneNumber}", phone.Number);
                await _supabaseService.AssignPhoneToHostAsync(phone.Id, _currentHost.Id);
                await StartPhoneContainerAsync(phone);
                currentCount++;
            }
            _logger.LogInformation("Container sync completed. Managing {Count} phones", currentCount);
        }
        finally { _syncLock.Release(); }
    }

    public async Task HealthCheckAllAsync()
    {
        if (_currentHost == null) return;
        try
        {
            var phones = await _supabaseService.GetPhonesForHostAsync(_currentHost.Id);
            foreach (var phone in phones.Where(p => p.DockerStatus == PhoneDockerStatus.Running))
            {
                if (string.IsNullOrEmpty(phone.ContainerId) || !phone.ApiPort.HasValue) continue;
                var isHealthy = await _dockerService.CheckHealthAsync(phone.ContainerId, phone.ApiPort.Value);
                if (!isHealthy)
                {
                    _logger.LogWarning("Phone {PhoneNumber} failed health check", phone.Number);
                    await _supabaseService.LogAgentEventAsync(_currentHost.Id, AgentEventType.HealthCheckFailed, new { phoneId = phone.Id });
                    await RestartPhoneContainerAsync(phone);
                }
                else
                {
                    await _supabaseService.UpdatePhoneDockerStatusAsync(phone.Id, PhoneDockerStatus.Running);
                }
            }
        }
        catch (Exception ex) { _logger.LogError(ex, "Error during health check"); }
    }

    public async Task TakeOverFromDeadHostAsync(Guid deadHostId)
    {
        if (_currentHost == null) return;
        try
        {
            _logger.LogWarning("Taking over phones from dead host {DeadHostId}", deadHostId);
            var phones       = await _supabaseService.GetPhonesForHostAsync(deadHostId);
            var currentCount = (await _supabaseService.GetPhonesForHostAsync(_currentHost.Id)).Count;
            var takenOver    = new List<Guid>();
            var skipped      = new List<Guid>();

            foreach (var phone in phones)
            {
                if (currentCount >= _hostSettings.MaxContainers) { skipped.Add(phone.Id); continue; }
                try
                {
                    await _supabaseService.AssignPhoneToHostAsync(phone.Id, _currentHost.Id);
                    var hasCredentials = !string.IsNullOrEmpty(phone.CredsBase64);
                    if (hasCredentials) await RestoreCredsAsync(phone);
                    var started = await StartPhoneContainerAsync(phone);
                    if (started)
                    {
                        takenOver.Add(phone.Id);
                        await _supabaseService.LogAgentEventAsync(_currentHost.Id, AgentEventType.Migrated,
                            new { action = "takeover", phoneId = phone.Id, phoneNumber = phone.Number, fromHostId = deadHostId, toHostId = _currentHost.Id, hadCredentials = hasCredentials, timestamp = DateTime.UtcNow });
                        currentCount++;
                    }
                }
                catch (Exception phoneEx) { _logger.LogError(phoneEx, "Error taking over phone {PhoneNumber}", phone.Number); }
            }

            await _supabaseService.SetHostStatusAsync(deadHostId, "inactive");
            await _supabaseService.LogAgentEventAsync(_currentHost.Id, AgentEventType.Migrated,
                new { action = "takeover_summary", fromHostId = deadHostId, toHostId = _currentHost.Id, totalPhones = phones.Count, takenOver = takenOver.Count, skipped = skipped.Count, timestamp = DateTime.UtcNow });

            _logger.LogInformation("Takeover complete: {TakenOver}/{Total} phones from host {DeadHostId}", takenOver.Count, phones.Count, deadHostId);
        }
        catch (Exception ex) { _logger.LogError(ex, "Error taking over from dead host {DeadHostId}", deadHostId); }
    }

    public async Task<bool> PausePhoneContainerAsync(Phone phone)
    {
        if (_currentHost == null) { _logger.LogError("Host not initialized"); return false; }
        try
        {
            _logger.LogInformation("Pausing phone {PhoneNumber}", phone.Number);
            if (!string.IsNullOrEmpty(phone.ContainerId))
            {
                await _dockerService.StopContainerAsync(phone.ContainerId);
                await _dockerService.RemoveContainerAsync(phone.ContainerId);
            }

            var phoneIndex   = phone.Number.Replace("+", "");
            var authPath     = Path.Combine(_dockerSettings.DataBasePath, $"auth_{phoneIndex}");
            var logsPath     = Path.Combine(_dockerSettings.DataBasePath, $"logs_{phoneIndex}");
            var contactsPath = Path.Combine(_dockerSettings.DataBasePath, $"contacts_{phoneIndex}");

            if (Directory.Exists(authPath))     Directory.Delete(authPath, recursive: true);
            if (Directory.Exists(logsPath))     Directory.Delete(logsPath, recursive: true);
            if (Directory.Exists(contactsPath)) Directory.Delete(contactsPath, recursive: true);

            await _supabaseService.UpdatePhoneDockerStatusAsync(phone.Id, PhoneDockerStatus.Stopped, containerId: "", containerName: "", dockerUrl: "");
            await _supabaseService.DetachPhoneFromHostAsync(phone.Id);
            await _supabaseService.LogAgentEventAsync(_currentHost.Id, AgentEventType.Stopped, new { phoneId = phone.Id, action = "pause", phoneNumber = phone.Number });

            _logger.LogInformation("Phone {PhoneNumber} paused and detached from host", phone.Number);
            return true;
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Error pausing phone {PhoneNumber}", phone.Number);
            await _supabaseService.UpdatePhoneDockerStatusAsync(phone.Id, PhoneDockerStatus.Error, errorMessage: ex.Message);
            return false;
        }
    }
}

record ContainerStatusResponse(string Status);
record WebhookListResponse(List<string> Webhooks, int Count);
