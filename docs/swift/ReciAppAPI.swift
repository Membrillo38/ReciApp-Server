import Foundation

// Names match JSONDecoder.convertFromSnakeCase (Url, not URL).
struct RecipeIngredient: Codable, Sendable {
    let name: String
    let quantity: String?
    let unit: String?
}
struct RecipeIngredientSection: Codable, Sendable {
    let title: String
    let ingredients: [RecipeIngredient]
}
struct RecipeStep: Codable, Sendable {
    let order: Int
    let text: String
    let durationMinutes: Int?
}
struct RecipeTip: Codable, Sendable {
    let title: String?
    let text: String
}
struct RecipePublic: Codable, Identifiable, Sendable {
    let id: UUID
    let title: String
    let ingredients: [RecipeIngredient]
    let ingredientSections: [RecipeIngredientSection]
    let steps: [RecipeStep]
    let servings: Int?
    let prepMinutes: Int?
    let cookMinutes: Int?
    let tags: [String]
    let sourceUrl: String
    let platform: String
    let thumbnailUrl: String?
    let carouselImageUrls: [String]
    let author: String?
    let description: String?
    let tips: [RecipeTip]
    let languageCode: String
}
struct RecipeSummary: Decodable, Identifiable, Sendable {
    let id: UUID
    let title: String
    let platform: String
    let sourceUrl: String
    let thumbnailUrl: String?
    let author: String?
    let servings: Int?
    let prepMinutes: Int?
    let cookMinutes: Int?
    // ISO 8601 text retained losslessly, including fractional seconds.
    let savedAt: String
    let languageCode: String
}
struct MeResponse: Decodable, Sendable {
    let id: UUID
    let displayName: String?
    let isPro: Bool
    let proExpiresAt: String?
    let freeUsedThisWeek: Int
    let freeLimit: Int
    let freeRemaining: Int
    let proRemainingCents: Double?
}
struct RecipeListResponse: Decodable, Sendable { let items: [RecipeSummary] }
struct ExtractRequest: Encodable, Sendable { let url: String; let language: String }
struct ExtractResponse: Decodable, Sendable {
    let jobId: UUID
    let status: String
    let cacheHit: Bool
    let progress: Int
    let queued: Bool?
    let queuePosition: Int?
}
struct JobResponse: Decodable, Sendable {
    let jobId: UUID
    let status: String
    let cacheHit: Bool
    let recipe: RecipePublic?
    let recipeId: UUID?
    let error: String?
    let progress: Int
    let nextJobId: UUID?
}
struct QueuedJobItem: Decodable, Sendable {
    let jobId: UUID
    let status: String
    let progress: Int
    let sourceUrl: String
    let queuePosition: Int
    let createdAt: String?
    let jobKind: String?
}
struct QueuedJobsResponse: Decodable, Sendable { let items: [QueuedJobItem] }
struct OkResponse: Decodable, Sendable { let ok: Bool }
struct ReciAPIError: Error, LocalizedError, Sendable {
    let status: Int
    let code: String?
    let message: String
    let requestID: String?
    let retryAfter: String?
    var errorDescription: String? { message }
}

final class ReciNoRedirectDelegate: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

actor ReciAppAPI {
    // refresh=true only after a rejected token; normal calls use auth.session.
    typealias TokenProvider = @Sendable (_ refresh: Bool) async throws -> String
    private let baseURL = URL(string: "https://api.acasillas.com/reciapp")!
    private let session: URLSession
    private let tokenProvider: TokenProvider

    init(session: URLSession = URLSession(configuration: .default, delegate: ReciNoRedirectDelegate(), delegateQueue: nil), tokenProvider: @escaping TokenProvider) {
        self.session = session
        self.tokenProvider = tokenProvider
    }

    private func request<T: Decodable & Sendable>(
        _ path: String, method: String = "GET", language: String? = nil,
        body: Data? = nil
    ) async throws -> T {
        var components = URLComponents(url: baseURL.appendingPathComponent(path), resolvingAgainstBaseURL: false)!
        if let language { components.queryItems = [URLQueryItem(name: "language", value: language)] }
        for attempt in 0...1 {
            try Task.checkCancellation()
            var request = URLRequest(url: components.url!)
            request.httpMethod = method
            request.timeoutInterval = 90 // Extract/poll can run long on the VPS.
            request.httpBody = body
            request.setValue("application/json", forHTTPHeaderField: "Accept")
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.setValue(UUID().uuidString, forHTTPHeaderField: "X-Request-ID")
            let token = try await tokenProvider(attempt == 1)
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else { throw URLError(.badServerResponse) }
            // A 401 rejects authentication before endpoint execution. Never retry
            // POST/DELETE on timeouts or 5xx: their write may already have committed.
            if http.statusCode == 401 && attempt == 0 { continue }
            guard (200..<300).contains(http.statusCode) else {
                let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
                let detail = object?["detail"]
                let fields = detail as? [String: Any]
                throw ReciAPIError(
                    status: http.statusCode, code: fields?["code"] as? String,
                    message: (detail as? String) ?? (fields?["message"] as? String) ?? "HTTP \(http.statusCode)",
                    requestID: http.value(forHTTPHeaderField: "X-Correlation-ID"),
                    retryAfter: http.value(forHTTPHeaderField: "Retry-After")
                )
            }
            let decoder = JSONDecoder()
            decoder.keyDecodingStrategy = .convertFromSnakeCase
            return try decoder.decode(T.self, from: data)
        }
        throw URLError(.userAuthenticationRequired)
    }

    func me() async throws -> MeResponse { try await request("v1/me") }
    func recipes(language: String) async throws -> RecipeListResponse {
        try await request("v1/me/recipes", language: language)
    }
    func recipe(id: UUID, language: String) async throws -> RecipePublic {
        try await request("v1/recipes/\(id.uuidString)", language: language)
    }
    func extract(url: URL, language: String) async throws -> ExtractResponse {
        // Dedupe: if this normalized URL was already imported locally, skip POST
        // unless the user explicitly re-imports. Server cache_hit is free but still
        // creates a completed job row — local skip saves a round trip.
        let body = try JSONEncoder().encode(ExtractRequest(url: url.absoluteString, language: language))
        return try await request("v1/extract", method: "POST", body: body)
    }
    func myJobs() async throws -> QueuedJobsResponse {
        try await request("v1/me/jobs")
    }
    func job(id: UUID, language: String) async throws -> JobResponse {
        try await request("v1/jobs/\(id.uuidString)", language: language)
    }
    func removeRecipe(id: UUID) async throws -> OkResponse {
        try await request("v1/me/recipes/\(id.uuidString)", method: "DELETE")
    }
    func deleteAccount() async throws -> OkResponse {
        try await request("v1/me", method: "DELETE")
    }
    // Persist the ID from extract() BEFORE calling this method. Persist each
    // handoff in onUpdate, so closing the app does not lose a translation job.
    func waitForRecipe(
        jobID: UUID, language: String,
        onUpdate: @Sendable (JobResponse) async -> Void = { _ in }
    ) async throws -> RecipePublic {
        var currentID = jobID
        let deadline = ContinuousClock.now.advanced(by: .seconds(600))
        while ContinuousClock.now < deadline {
            try Task.checkCancellation()
            let state = try await job(id: currentID, language: language)
            await onUpdate(state)
            currentID = state.nextJobId ?? state.jobId
            if state.status == "failed" {
                throw ReciAPIError(status: 200, code: "JOB_FAILED", message: state.error ?? "Import failed", requestID: nil, retryAfter: nil)
            }
            if state.status == "completed", let recipe = state.recipe { return recipe }
            guard ["pending", "processing", "completed"].contains(state.status) else {
                throw URLError(.cannotParseResponse)
            }
            try await Task.sleep(for: .seconds(2))
        }
        // This ends waiting, not the server job. Resume with the persisted ID.
        throw URLError(.timedOut)
    }
}
