//! `psr` — the Rust scraper CLI (v1: generic-path refresh milestone).
//!
//! Subcommands:
//! - `env-check`: validates the ingestion environment variables.
//! - `targets`: select refresh targets from a projected target JSON file.
//! - `probe`: fetch one URL through the generic walk and print the outcome.
//! - `refresh`: run the refresh orchestration over a target file.
//! - `catalog-fetch`: dedicated reader discovery (phase 3 placeholder).
//! - `ingest`: send prepared observations through the ingestion client.
//!
//! The CLI never ingests an unverified observation: `refresh` output rows
//! carry `identity_verified` and `ingest` requires an explicit payload file
//! the operator prepared after the identity gate.

use std::io::Read;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use clap::{Parser, Subcommand};
use psr_core::{select_offer_targets, TargetRow};
use psr_ingestion::IngestionClient;

#[derive(Parser)]
#[command(name = "psr", version, about = "Teknoskor product scraper (Rust v1)")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Validate the ingestion environment.
    EnvCheck,
    /// Select refresh targets from a projected JSON file (max 12).
    Targets {
        /// JSON file with an array of projected target rows.
        #[arg(value_parser)]
        file: PathBuf,
    },
    /// Fetch one URL through the generic walk and print the outcome.
    Probe {
        #[arg(long)]
        url: String,
        /// Relax the URL trust gate (wiremock/plain-HTTP probes only).
        #[arg(long, default_value_t = false)]
        permissive: bool,
    },
    /// Run the refresh orchestration over a target file.
    Refresh {
        /// JSON file with `{"product_name": …, "specs": {…}, "targets": [...]}`.
        #[arg(value_parser)]
        file: PathBuf,
    },
    /// Dedicated reader discovery (phase 3 placeholder).
    CatalogFetch {
        #[arg(long)]
        product: String,
    },
    /// Send a prepared offers payload through the ingestion client.
    Ingest {
        /// JSON file with the prepared offers payload.
        #[arg(value_parser)]
        file: PathBuf,
        /// Product id for listing-scoped ingestion.
        #[arg(long)]
        product_id: Option<String>,
        /// Allow plain-HTTP ingestion endpoints (wiremock only).
        #[arg(long, default_value_t = false)]
        insecure: bool,
    },
}

fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("warn")),
        )
        .init();
    let cli = Cli::parse();
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(4)
        .enable_all()
        .build()
        .expect("tokio runtime");
    if let Err(error) = runtime.block_on(run(cli.command)) {
        eprintln!("error: {error}");
        std::process::exit(1);
    }
}

fn http_client() -> reqwest::Client {
    reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(Duration::from_secs(5))
        .timeout(Duration::from_secs(20))
        .build()
        .expect("http client")
}

async fn run(command: Command) -> Result<(), String> {
    match command {
        Command::EnvCheck => env_check().await,
        Command::Targets { file } => targets(&file).await,
        Command::Probe { url, permissive } => probe(&url, permissive).await,
        Command::Refresh { file } => refresh(&file).await,
        Command::CatalogFetch { product } => {
            // Phase 3: dedicated source readers (hepsiburada, trendyol, n11,
            // teknosa, mediamarkt, apple, turktelekom, pasaj) plus Pazarama
            // category discovery.
            Err(format!(
                "catalog-fetch for {product:?} is phase-3 work: dedicated_reader_pending"
            ))
        }
        Command::Ingest {
            file,
            product_id,
            insecure,
        } => ingest(&file, product_id.as_deref(), insecure).await,
    }
}

async fn env_check() -> Result<(), String> {
    let mut problems = Vec::new();
    let url = std::env::var("TEKNOSKOR_INGESTION_URL").unwrap_or_default();
    if url.is_empty() {
        problems.push("TEKNOSKOR_INGESTION_URL is not set".to_string());
    } else if !url.starts_with("https://") {
        problems.push("TEKNOSKOR_INGESTION_URL must be an https:// base URL".to_string());
    }
    let secret = std::env::var("SCRAPER_INGESTION_SECRET").unwrap_or_default();
    if secret.chars().count() < 32 {
        problems.push(
            "SCRAPER_INGESTION_SECRET must contain at least 32 characters".to_string(),
        );
    }
    if problems.is_empty() {
        println!("env ok: TEKNOSKOR_INGESTION_URL={url}");
        Ok(())
    } else {
        Err(problems.join("; "))
    }
}

async fn read_json(file: &PathBuf) -> Result<serde_json::Value, String> {
    let mut text = String::new();
    if file.as_os_str() == "-" {
        std::io::stdin()
            .read_to_string(&mut text)
            .map_err(|error| format!("stdin: {error}"))?;
    } else {
        text = std::fs::read_to_string(file)
            .map_err(|error| format!("{}: {error}", file.display()))?;
    }
    serde_json::from_str(&text).map_err(|error| format!("{}: {error}", file.display()))
}

async fn targets(file: &PathBuf) -> Result<(), String> {
    let payload = read_json(file).await?;
    let rows: Vec<TargetRow> = serde_json::from_value(payload)
        .map_err(|error| format!("targets file: {error}"))?;
    let row_values: Vec<serde_json::Value> = rows
        .iter()
        .map(serde_json::to_value)
        .collect::<Result<_, _>>()
        .map_err(|error| error.to_string())?;
    let selected = select_offer_targets(&row_values, psr_refresh::MAX_TARGETS_PER_REFRESH);
    let skipped = rows.len() - selected.len();
    println!(
        "{}",
        serde_json::to_string_pretty(&serde_json::json!({
            "input": rows.len(),
            "selected": selected,
            "skipped": skipped,
        }))
        .map_err(|error| error.to_string())?
    );
    Ok(())
}

async fn probe(url: &str, permissive: bool) -> Result<(), String> {
    let engine = if permissive {
        FetchEngine::permissive_for_tests(http_client())
    } else {
        FetchEngine::new(http_client())
    };
    let offer = engine.fetch_target(url).await;
    let attempt = engine.diagnostics(url).await;
    println!(
        "{}",
        serde_json::to_string_pretty(&serde_json::json!({
            "url": url,
            "offer": offer,
            "attempt": attempt,
        }))
        .map_err(|error| error.to_string())?
    );
    Ok(())
}

async fn refresh(file: &PathBuf) -> Result<(), String> {
    let payload = read_json(file).await?;
    let product_name = payload
        .get("product_name")
        .and_then(|value| value.as_str())
        .ok_or("payload needs product_name")?
        .to_string();
    let targets: Vec<TargetRow> = serde_json::from_value(
        payload
            .get("targets")
            .cloned()
            .ok_or("payload needs targets")?,
    )
    .map_err(|error| format!("targets: {error}"))?;
    let specs: psr_refresh::ExpectedSpecs = match payload.get("specs") {
        Some(value) => serde_json::from_value(value.clone())
            .map_err(|error| format!("specs: {error}"))?,
        None => psr_refresh::ExpectedSpecs::default(),
    };

    let engine = Arc::new(FetchEngine::new(http_client()));
    let report =
        psr_refresh::refresh_report(&engine, &targets, &product_name, &specs).await;
    let unverified = report.iter().filter(|row| !row.identity_verified).count();
    let observed = report.iter().filter(|row| row.offer.is_some()).count();
    println!(
        "{}",
        serde_json::to_string_pretty(&serde_json::json!({
            "product_name": product_name,
            "observed": observed,
            "unverified": unverified,
            "rows": report,
        }))
        .map_err(|error| error.to_string())?
    );
    if unverified > 0 {
        // Fail closed: unverified observations are diagnostics only.
        return Err(format!(
            "{unverified} observation(s) unverified — identity matching is phase-2 work; \
             run the Python identity gate before ingesting"
        ));
    }
    Ok(())
}

async fn ingest(
    file: &PathBuf,
    product_id: Option<&str>,
    insecure: bool,
) -> Result<(), String> {
    let payload = read_json(file).await?;
    let offers: Vec<serde_json::Value> = serde_json::from_value(payload)
        .map_err(|error| format!("offers payload must be a JSON array: {error}"))?;
    let client = if insecure {
        IngestionClient::new_insecure_for_tests(
            &std::env::var("TEKNOSKOR_INGESTION_URL").unwrap_or_default(),
            &std::env::var("SCRAPER_INGESTION_SECRET").unwrap_or_default(),
        )
    } else {
        IngestionClient::from_env()
    }
    .map_err(|error| error.to_string())?;
    let result = match product_id {
        Some(product_id) => {
            println!("note: product_id {product_id} ignored — ingest_listing_offers is URL-keyed");
            client.ingest_listing_offers(&offers).await
        }
        None => client.ingest_offers(&offers, true, false).await,
    };
    let result = result.map_err(|error| error.to_string())?;
    println!(
        "{}",
        serde_json::to_string_pretty(&result).map_err(|error| error.to_string())?
    );
    Ok(())
}

use psr_fetch::FetchEngine;
