using System.Net.Http.Headers;

namespace VRChatVideoPlayerEnhancer;

internal static class Program
{
    private const string BaseUrl = "http://127.0.0.1:9696";

    private static string LogPath()
    {
        var tools = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData) + "Low",
            "VRChat", "VRChat", "Tools");
        Directory.CreateDirectory(tools);
        return Path.Combine(tools, "ytdl-enhancer.log");
    }

    private static void Log(string path, string message)
    {
        try { File.AppendAllText(path, $"{DateTime.Now:yyyy-MM-dd HH:mm:ss} {message}{Environment.NewLine}"); }
        catch { /* Diagnostics must never change yt-dlp's contract. */ }
    }

    public static async Task<int> Main(string[] args)
    {
        var logPath = LogPath();
        var url = args.FirstOrDefault(arg =>
            arg.StartsWith("http://", StringComparison.OrdinalIgnoreCase) ||
            arg.StartsWith("https://", StringComparison.OrdinalIgnoreCase));
        var avpro = !args.Any(arg => arg.Contains("[protocol^=http]", StringComparison.Ordinal));
        var source = args.Any(arg => arg.Contains("--flat-playlist", StringComparison.Ordinal))
            ? "resonite" : "vrchat";
        Log(logPath, $"Starting avpro={avpro} source={source} url={url ?? "<none>"}");

        if (url is null)
        {
            await Console.Error.WriteLineAsync("ERROR: No URL found in yt-dlp arguments");
            return 1;
        }

        try
        {
            using var client = new HttpClient();
            client.DefaultRequestHeaders.UserAgent.Add(new ProductInfoHeaderValue("VRChat-Video-Player-Enhancer", "1.0"));
            var endpoint = $"{BaseUrl}/api/getvideo?url={Uri.EscapeDataString(url)}&avpro={avpro.ToString().ToLowerInvariant()}&source={source}";
            using var response = await client.GetAsync(endpoint);
            var output = await response.Content.ReadAsStringAsync();
            Log(logPath, $"Response status={(int)response.StatusCode}: {output.Trim()}");
            if (!response.IsSuccessStatusCode)
            {
                await Console.Error.WriteLineAsync(output.Trim());
                return 1;
            }
            Console.WriteLine(output.Trim());
            return 0;
        }
        catch (Exception ex)
        {
            Log(logPath, $"Error {ex.GetType().Name}: {ex.Message}");
            await Console.Error.WriteLineAsync($"ERROR: {ex.Message}");
            return 1;
        }
    }
}
