using System.Diagnostics;
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

    private static string? FindUrl(string[] args)
    {
        for (var index = args.Length - 1; index >= 0; index--)
        {
            if (Uri.TryCreate(args[index], UriKind.Absolute, out var candidate) &&
                (candidate.Scheme.Equals("http", StringComparison.OrdinalIgnoreCase) ||
                 candidate.Scheme.Equals("https", StringComparison.OrdinalIgnoreCase)))
            {
                return args[index];
            }
        }

        return null;
    }

    private static bool IsYouTubeUrl(string value)
    {
        if (!Uri.TryCreate(value, UriKind.Absolute, out var uri) ||
            (!uri.Scheme.Equals("http", StringComparison.OrdinalIgnoreCase) &&
             !uri.Scheme.Equals("https", StringComparison.OrdinalIgnoreCase)))
        {
            return false;
        }

        var host = uri.Host.TrimEnd('.');
        return host.Equals("youtu.be", StringComparison.OrdinalIgnoreCase) ||
            host.Equals("youtube.com", StringComparison.OrdinalIgnoreCase) ||
            host.EndsWith(".youtube.com", StringComparison.OrdinalIgnoreCase);
    }

    private static string OriginalExecutablePath()
    {
        var executableDirectory = Path.GetDirectoryName(Environment.ProcessPath);
        return Path.Combine(executableDirectory ?? AppContext.BaseDirectory, "yt-dlp.exe.bkp");
    }

    private static async Task<int> RunOriginalAsync(string[] args, string logPath)
    {
        var originalPath = OriginalExecutablePath();
        if (!File.Exists(originalPath))
        {
            const string message = "ERROR: Original VRChat yt-dlp backup is missing; cannot handle this non-YouTube URL";
            Log(logPath, $"{message}: {originalPath}");
            await Console.Error.WriteLineAsync(message);
            return 1;
        }

        var startInfo = new ProcessStartInfo
        {
            FileName = originalPath,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
        };
        foreach (var argument in args)
        {
            startInfo.ArgumentList.Add(argument);
        }

        Log(logPath, $"Passing non-YouTube request to original yt-dlp: {originalPath}");
        try
        {
            using var process = Process.Start(startInfo);
            if (process is null)
            {
                await Console.Error.WriteLineAsync("ERROR: Could not start original VRChat yt-dlp");
                return 1;
            }

            var stdout = process.StandardOutput.ReadToEndAsync();
            var stderr = process.StandardError.ReadToEndAsync();
            await process.WaitForExitAsync();
            Console.Write(await stdout);
            Console.Error.Write(await stderr);
            Log(logPath, $"Original yt-dlp exited with status {process.ExitCode}");
            return process.ExitCode;
        }
        catch (Exception ex)
        {
            Log(logPath, $"Original yt-dlp error {ex.GetType().Name}: {ex.Message}");
            await Console.Error.WriteLineAsync($"ERROR: {ex.Message}");
            return 1;
        }
    }

    public static async Task<int> Main(string[] args)
    {
        var logPath = LogPath();
        var url = FindUrl(args);
        var avpro = !args.Any(arg => arg.Contains("[protocol^=http]", StringComparison.Ordinal));
        var source = args.Any(arg => arg.Contains("--flat-playlist", StringComparison.Ordinal))
            ? "resonite" : "vrchat";
        Log(logPath, $"Starting avpro={avpro} source={source} url={url ?? "<none>"}");

        if (url is null || !IsYouTubeUrl(url))
        {
            return await RunOriginalAsync(args, logPath);
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
