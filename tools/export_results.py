"""Export/validate the organizer's submission JSON 1.0 without exposing source data."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.results_export import export_results, validate_submission  # noqa: E402
from server.evaluation import PROMPT_REVISION, think_enabled  # noqa: E402
from server.storage import Store  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', required=True, choices=('dev', 'prod'))
    parser.add_argument('--group-id', help='Exact dataset_group_id returned by the API')
    parser.add_argument('--team-name', help='Actual team name, required for export')
    parser.add_argument('--database', type=Path, default=ROOT / '.local' / 'cqa.sqlite3')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--reviewed', action='store_true', help='Explicitly export human-reviewed final answers')
    parser.add_argument('--include-external', action='store_true',
                        help='Include verified Codex CLI external evaluations; report actual hybrid/external metadata')
    parser.add_argument('--require-complete', action='store_true', help='Reject any status=error row')
    parser.add_argument('--model-name', help='Actual attempted model if no successful evaluation exists')
    parser.add_argument('--model-filter', help='Require this recorded provider model')
    parser.add_argument('--prompt-revision', help='Require this recorded prompt revision')
    parser.add_argument('--think', choices=('0', '1'), help='Require this recorded thinking setting')
    parser.add_argument('--current-provider', action='store_true',
                        help='Require configured CQA_MODEL, current official prompt revision and CQA_THINK')
    parser.add_argument('--validate-file', type=Path, help='Validate existing JSON; no database is needed')
    args = parser.parse_args()
    try:
        if args.validate_file:
            payload = json.loads(args.validate_file.read_text(encoding='utf-8-sig'))
            validate_submission(payload, dataset=args.dataset, require_complete=args.require_complete)
        else:
            if not args.team_name:
                parser.error('--team-name is required for export')
            group = args.group_id or ('DEV' if args.dataset == 'dev' else None)
            if not group:
                parser.error('--group-id is required for PROD')
            if not args.database.exists():
                raise ValueError('Application database does not exist; import source conversations first')
            provider_filter = ({'model': os.environ.get('CQA_MODEL', '').strip(),
                                'prompt_revision': PROMPT_REVISION, 'think': think_enabled()}
                               if args.current_provider else {})
            if args.model_filter:
                provider_filter['model'] = args.model_filter
            if args.prompt_revision:
                provider_filter['prompt_revision'] = args.prompt_revision
            if args.think is not None:
                provider_filter['think'] = args.think == '1'
            payload = export_results(Store(args.database), args.dataset, group, args.team_name,
                                     reviewed=args.reviewed, require_complete=args.require_complete,
                                     model_name=args.model_name, provider_filter=provider_filter,
                                     include_external=args.include_external)
            output = args.output or ROOT / 'results' / args.dataset / 'results.json'
            output = output.resolve()
            if not output.is_relative_to(ROOT):
                raise ValueError('Output must stay inside the project directory')
            output.parent.mkdir(parents=True, exist_ok=True)
            temp = output.with_suffix(output.suffix + '.tmp')
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
            temp.replace(output)
        errors = sum(row['status'] == 'error' for row in payload['results'])
        print(f'Valid JSON 1.0: {len(payload["results"])} records; {errors} error rows; '
              f'snapshot={"reviewed" if args.reviewed else "original model"}.')
        return 0
    except (ValueError, OSError, TypeError) as error:
        # JSON/provider/database internals can contain signed URLs or transcripts.
        if isinstance(error, json.JSONDecodeError):
            print('Invalid JSON document.', file=sys.stderr)
        elif isinstance(error, OSError):
            print('Unable to read or write the selected local file.', file=sys.stderr)
        else:
            print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
