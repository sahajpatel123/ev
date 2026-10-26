import Foundation

/// Check callback with a defaulted detail string (`callAsFunction` allows
/// what a plain closure type cannot: `check("name", cond)` and
/// `check("name", cond, "detail")` both compile).
struct CheckFn {
    let run: (String, Bool, String) -> Void
    func callAsFunction(_ name: String, _ cond: Bool, _ detail: String = "") {
        run(name, cond, detail)
    }
}
