# Scraper Agent Flow Documentation

This document describes the high-level data flow for when the UI has a new scraper design and the embedded agent handles the code generation.

## Overview

The k-realty-agent is an LLM agent/harness that automatically generates domain-specific processing code (parsers and scrapers) through its embedded agent. When a new scraper design is needed (such as when a new portal source is added), the system follows a specific flow to build the scraper code.

## Flow for New Scraper Design

### 1. User Initiation
- User interacts with the web UI to add a new source that requires portal scraping
- The user demonstrates the portal navigation via browser (recording actions in `demo_recorder.py`)
- The demonstration includes: login sequence, filter settings, and data generation click
- This is captured as a demonstration JSON file

### 2. Scraper Build Process

The flow starts when the user clicks "Build Scraper" in the UI, which calls `start_build` function in `interfaces/mcp_tools.py`.

#### Key Components:
- `start_build()` in `interfaces/mcp_tools.py` (line 1744) - initiates the process
- `build_scraper_for_source()` in `orchestration/build_scraper.py` (line 44) - orchestrates the scraper building
- `run_codegen_gated()` in `orchestration/codegen.py` (line 282) - runs the agent code generation with verification
- The embedded agent using prompts from `core/prompts/`

#### Process Flow:

1. **Demonstration Capture**
   - User completes a browser demonstration (login → filter → generate data)
   - The demonstration is recorded in `core/tools/demo_recorder.py`
   - Captures network requests and browser actions

2. **Agent Prompt Selection**
   - Uses `scraper_contract.v1.md` (contract defines what a scraper must be)
   - Uses `scraper_builder.v1.md` (instructions for building a new scraper)
   - Prompt combination is passed to the embedded agent

3. **Agent Execution**
   - The embedded agent (`orchestration/agent.py`) analyzes the demonstration
   - Finds data endpoint (preferred) or browser actions (fallback)
   - Generates code in `core/scrapers/<source_key>.py`
   - Writes the scraper with proper structure:
     - Implements `retrieve()` function returning `list[Transaction]`
     - Follows the scraper contract (contractually defined behavior)
     - Includes proper settings declaration
     - Implements reconciliation control total

4. **Code Verification**
   - The agent writes a self-contained test file (`tests/test_scraper_<key>.py`)
   - Verifies the test passes independently via `orchestration/verify.py`
   - Gate checks include:
     - Test exists and passes
     - Test actually exercises the changed code
     - No hardcoded configuration values
     - No code that does nothing
     - Agent actually wrote something
     - Agent doesn't end itself prematurely

5. **Registration**
   - Updates `core/scrapers/__init__.py` to import and register the scraper
   - Scraper becomes available in the service manifest

### 3. Verification and Approval

#### UI Presentation for Human Review

**Important Note**: The actual user interface behavior is slightly different than what the documentation previously stated. While the agent does generate the scraper and test, the UI review workflow is more nuanced:

1. **Automatic Testing**: After the agent builds a scraper, it automatically:
   - Writes a self-contained test file (`tests/test_scraper_<key>.py`)
   - Runs the test independently using `orchestration/verify.py`
   - If the test passes, the scraper is registered and ready for use

2. **UI Status Indicators**: The UI shows the scraper's status through:
   - A "needs approval" tag that appears when a scraper is built but not yet activated
   - The "Teach the harness this website" text appears when no scraper exists
   - Status indicators like "scraper" and "needs approval" tags in the source list

3. **Approval Process**: 
   - When the system shows "needs approval", this indicates the scraper is built but not activated
   - The user must explicitly approve the scraper in the UI to activate it
   - The UI shows a "Approve the parser" button in the source panel when a scraper is built

4. **No Direct Code Display**: Unlike some systems that display the actual generated code for review, this system focuses on status indicators and approval workflow:
   - The agent-generated code is automatically stored and tested
   - The UI shows that a scraper has been built via status indicators
   - The user must approve activation rather than review the generated code directly

#### The Approval Flow:

1. **Initial State**: User adds a new website source → System shows "Teach the harness this website"
2. **Build Process**: User demonstrates portal actions → Agent builds scraper and test
3. **Activation Status**: Scraper is built and registered → UI shows "needs approval" tag
4. **Approval**: User clicks "Approve" button → Scraper is activated for normal use

### 4. Verification and Approval

1. **Independent Testing**
   - The harness runs the generated test independently in a fresh subprocess
   - Test validation ensures the generated code is correct
   - If test fails, the build process is considered not complete

2. **UI Presentation for Review**
   - The UI shows "needs approval" tag on sources with newly built scrapers
   - The "Teach the harness this website" message appears for sources with no scraper
   - When a scraper is built but not activated, the UI displays activation controls

3. **Activation**
   - Operator can approve the scraper through the "Approve the parser" button
   - The approval makes the scraper active and available for regular use
   - Until approval, the scraper is in "pending activation" state

## Key Files Involved

### Core Agent Components
- `orchestration/agent.py` - The embedded agent loop implementation
- `orchestration/codegen.py` - Code generation primitives with verification
- `orchestration/build_scraper.py` - Scraper building workflow
- `orchestration/verify.py` - Verification gates for generated code

### Prompt System
- `core/prompts/scraper_contract.v1.md` - Defines the contract and requirements
- `core/prompts/scraper_builder.v1.md` - Instructions for building new scrapers
- `core/prompts/scraper_reviser.v1.md` - Instructions for fixing existing scrapers

### Scraper Architecture
- `core/scrapers/base.py` - Scraper contract definition
- `core/scrapers/__init__.py` - Registry of scrapers
- `tests/test_scraper_<key>.py` - Generated tests (created by the agent)

## Data Flow Summary

1. **Input**: User demonstration of portal navigation
2. **Processing**: Agent analyzes demonstration, builds scraper code  
3. **Output**: 
   - Generated scraper code in `core/scrapers/<key>.py`
   - Self-contained test in `tests/test_scraper_<key>.py`
   - Registered scraper in the system
4. **Validation**: Test runs independently to verify correctness
5. **Approval**: Operator approves the built scraper (via UI controls)

## Agent Workflow Rules

1. **Faithful Transactions**: All data extraction matches source's actual columns
2. **Settings Declaration**: All configurable options are declared properly
3. **Reconciliation**: Scraper must check its own arithmetic
4. **Self-Contained Tests**: Generated code requires tests that run independently
5. **Framework-Free**: Generated code must not import langgraph/langchain
6. **Code Generation**: The embedded agent writes the code, not developers

## UI Integration

The UI flow is tightly integrated with the agent workflow:
1. **Source Creation**: User adds a new source via the wizard
2. **Demonstration**: User performs portal navigation demonstration
3. **Build Initiation**: Agent build process starts automatically or on user request
4. **Review**: UI shows status indicators for built scrapers ("needs approval")
5. **Approval**: Operator approves the scraper to activate it

## Verification Gates

The agent-generated scraper must pass all verification gates in `orchestration/verify.py`:
- Test exists and passes
- Test actually executes changed code
- No hardcoded configuration
- No dead code
- Code actually writes data
- Agent doesn't terminate prematurely

## Clarification on Code Review Display

**Important**: Contrary to initial assumptions, the UI does NOT show the actual generated scraper code for direct human review. Instead:

- The agent generates and automatically tests the scraper code
- The UI presents status indicators: "needs approval", "scraper", etc.
- When a scraper is built but not activated, a "Approve the parser" button appears
- The actual generated code is stored and used automatically after approval
- The human reviewer focuses on verifying functionality rather than examining implementation details

This approach prioritizes workflow efficiency and clear status communication over exposing internal code implementation details to the user.

This approach ensures that scrapers are built consistently, tested thoroughly, and don't contain errors that would break the financial data processing pipeline.