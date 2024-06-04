import os
import pandas as pd
from sklearn.model_selection import KFold
import openai
from openai import OpenAI
import random
import time
import ast

def retry_with_exponential_backoff(
        func,
        initial_delay: float = 1,
        exponential_base: float = 2,
        jitter: bool = True,
        max_retries: int = 10,
        errors: tuple = (openai.RateLimitError,),
):
    """Retry a function with exponential backoff."""

    def wrapper(*args, **kwargs):
        num_retries = 0
        delay = initial_delay

        while True:
            try:
                return func(*args, **kwargs)
            except errors as e:
                num_retries += 1
                if num_retries > max_retries:
                    raise Exception(
                        f"Maximum number of retries ({max_retries}) exceeded."
                    )
                delay *= exponential_base * (1 + jitter * random.random())
                time.sleep(delay)

            except Exception as e:
                raise e

    return wrapper

def create_splits(file_path):
    '''
    A method that loads the full humanly coded dataset and splits it into "number_of_splits" different splits.
    For each split, 80% of the data is used for training, and 20% is used for testing. The test split is saved twice,
    once with the coding removed, and once with the coding still included for evaluation.
    '''
    number_of_splits = 5
    data = pd.read_excel(file_path)

    kf = KFold(n_splits=number_of_splits, shuffle=True, random_state=42)

    train_splits = []
    test_splits = []
    true_splits = []

    for train_index, test_index in kf.split(data):
        train = data.iloc[train_index]
        true = data.iloc[test_index]

        test = true.drop(['Main theme', 'Subtheme', 'Confidence level', 'Multiple themes', 'Comments'], axis=1)

        train_splits.append(train)
        test_splits.append(test)
        true_splits.append(true)

    for split_no in range(len(train_splits)):
        train_splits[split_no].to_excel('data/splits/dd-train_split_' + str(split_no) + '.xlsx', index=False)
        test_splits[split_no].to_excel('data/splits/dd-test_split_' + str(split_no) + '.xlsx', index=False)
        true_splits[split_no].to_excel('data/splits/dd-true_split_' + str(split_no) + '.xlsx', index=False)

def load_splits():
    '''
    Method that loads the dataframes of the human coding of the train samples, and the uncoded test samples for each
    split.

    :return: train_splits, test_splits - list dataframes of human coding and GPT predictions for each split.
    '''

    train_splits = []
    test_splits = []

    for split_no in range(5):
        train_splits.append(pd.read_excel('data/splits/dd-train_split_' + str(split_no) + '.xlsx'))
        test_splits.append(pd.read_excel('data/splits/dd-test_split_' + str(split_no) + '.xlsx'))

    return train_splits, test_splits

def load_eval_splits(prompt_version=""):
    '''
    Method that loads the dataframes of the human coding and GPT predictions for each split.

    :param prompt_version:  Suffix to be added to prompt file and prediction output, indicating the prompt version.
    :return: true_splits, pred_splits - list dataframes of human coding and GPT predictions for each split.
    '''
    true_splits = []
    pred_splits = []

    for split_no in range(5):
        true_splits.append(pd.read_excel('data/splits/dd-true_split_' + str(split_no) + '.xlsx'))
        pred_splits.append(pd.read_excel('results/dd-pred_split_' + str(split_no) + prompt_version + '.xlsx'))

    return true_splits, pred_splits

def make_prediction_xlsx(response_rows, path):
    '''
    Turn the list of GPT predictions into an .xlsx file.

    :param response_rows: The list of predictions generated by GPT.
    :param path: Path where the .xlsx file will be saved.
    '''
    pred_df = pd.DataFrame(
        columns=['Text', 'Main theme', 'Subtheme', 'Confidence level', 'Multiple themes', 'Comments'])
    for response_row in response_rows:
        if len(response_row) == 0:
            continue
        tempdf = pd.DataFrame([ast.literal_eval(response_row.replace(": nan,", ": \'\',")
                                                .replace(": 'nan',", ": \'\',").replace("'s", "\\'s"))])
        pred_df = pd.concat([pred_df, tempdf], ignore_index=True)

    pred_df.to_excel(path, index=False)

def call_gpt(train_splits, test_splits, prompt_version="", use_unseen_data=False):
    '''
    A function that loads the already labelled training data, and the test data that should be labelled.
    It creates a prompt based on a template and adds some examples of human coding, as well as uncoded sentences
    with the task to recreate the labelling. The output is saved as a .txt and .xlsx file.

    :param prompt_version: Suffix to be added to prompt file and prediction output, indicating the prompt version.
    '''
    num_example_sentences = 15
    num_new_sentences = 10

    with open("data/dd-gpt-prompt" + prompt_version + ".txt") as f:
        prompt_txt = f.read()

    @retry_with_exponential_backoff
    def chatcompletions_with_backoff(**kwargs):
        '''
        Helper function to deal with the "openai.error.RateLimitError". If not used, the script will simply
        stop once the limit is reached, not saving any of the data generated until then. This method will wait
        and then try again, hence preventing the error.

        :param kwargs: List of arguments passed to the OpenAI API for completion.
        '''
        return CLIENT.chat.completions.create(**kwargs)

    CLIENT = OpenAI(
        api_key=os.environ["OPENAI_API_KEY"], organization=os.environ["OPENAI_ORG"]
    )

    for split_no in range(len(train_splits)):
        print("  Processing split " + str(split_no+1) + "/" + str(len(train_splits)))

        prompt_beginning = prompt_txt
        for index, row in train_splits[split_no].iterrows():
            if index == num_example_sentences:
                break
            prompt_beginning += str(row.to_dict()) + "\n"

        prompt_beginning += '''
################################

Please do the same for the following sentences and complete the coding.

################################

'''

        prompt = ""
        response_rows = []

        for index, row in test_splits[split_no].iterrows():
            row_dict = row.to_dict()
            filtered_row_dict = {k: v for k, v in row_dict.items() if pd.notna(v)}
            prompt = prompt + str(filtered_row_dict) + "\n"

            if (index+1) % num_new_sentences == 0 or index == len(test_splits[split_no])-1:
                print("    Rows " + str(index+1) + "/" + str(len(test_splits[split_no])))

                prompt = prompt_beginning + prompt

                # Send the prompt to GPT
                response = CLIENT.chat.completions.create(
                    model="gpt-3.5-turbo", #"gpt-3.5-turbo", #
                    messages=[
                        {
                            "role": "user",
                            "content": prompt,
                        }
                    ],
                    temperature=0.3,
                    max_tokens=2250,
                    top_p=1.0,
                    frequency_penalty=0.0,
                    presence_penalty=0.0
                )

                response_text = response.choices[0].message.content.strip()

                response_rows.extend(response_text.split('\n'))

                prompt = ""

            if index == len(test_splits[split_no])-1:
                if use_unseen_data:
                    split_name = "unseen"
                else:
                    split_name = str(split_no)

                with open(r'results/dd-pred_split_' + split_name + prompt_version + '.txt', 'w') as fp:
                    fp.write('\n'.join(response_rows))

                make_prediction_xlsx(response_rows, 'results/dd-pred_split_' + split_name + prompt_version + '.xlsx')

                response_rows = []

def evaluate_splits(prompt_version=""):
    '''
    Method that creates a list of the themes and sub-themes of each sample and returns them as a list.

    :param prompt_version: Suffix to be added to prompt file and prediction output, indicating the prompt version.
    '''
    true_splits, pred_splits = load_eval_splits(prompt_version)

    text = []
    true_themes = []
    true_subthemes = []
    pred_themes = []
    pred_subthemes = []
    for split_no in range(len(true_splits)):
        # print("Evaluating split " + str(split_no + 1) + "/" + str(len(true_splits)))
        for row_true, row_pred in zip(true_splits[split_no].itertuples(), pred_splits[split_no].itertuples()):

            def extract_themes(row, themes, subthemes, add_text):
                '''
                Look through one row of the true or predicted dataframe and add the themes and subthemes to the list
                of themes for each sample. The text should only be added once (during the processing of the true or
                predicted dataframe),

                :param row: The row of the current sample read from the .xlsx file.
                :param themes: The list of themes for each sample, for the true or predicted coding results.
                :param subthemes: The list of subthemes for each sample, for the true or predicted coding results.
                :param add_text: Boolean to control whether the text should be added to the text list or not.
                '''
                if add_text:
                    text.append(row.Text)
                if row._5 != row._5: # "_5" = "Multiple Themes"; Check if value is "nan"
                    themes.append([row._2])
                    subthemes.append([row.Subtheme])
                else:
                    tt = []
                    ts = []
                    cats = row._5.strip().split("\n")
                    for cat in cats:
                        if "." in cat:
                            ts.append(cat)
                        else:
                            tt.append(cat)
                    themes.append(tt)
                    subthemes.append(ts)

            extract_themes(row_true, true_themes, true_subthemes, True)
            extract_themes(row_pred, pred_themes, pred_subthemes, False)

    def get_score(list_A, list_B):
        '''
        Calculate accuracy, recall, precision, and f1 scores by comparing the true and predicted coding results.

        :param list_A: List of the true themes or subthemes.
        :param list_B: List of the predicted themes or subthemes.
        :return: accuracy, recall, precision, f1 - evaluation metrics generated by comparing the two lists.
        '''
        true_positives = 0
        false_positives = 0
        false_negatives = 0

        for sublist_A, sublist_B in zip(list_A, list_B):
            set_A = set(sublist_A)
            set_B = set(sublist_B)

            true_positives += len(set_A & set_B)
            false_negatives += len(set_A - set_B)
            false_positives += len(set_B - set_A)

            accuracy = true_positives / (true_positives + false_positives + false_negatives)
            recall = true_positives / (true_positives + false_negatives)
            precision = true_positives / (true_positives + false_positives)
            f1 = 2 * (precision * recall) / (precision + recall)

        return accuracy, recall, precision, f1

    theme_accuracy, theme_precision, theme_recall, theme_f1 = get_score(true_themes, pred_themes)

    print(f"  Theme Accuracy: {theme_accuracy:.4f}")
    print(f"  Theme Precision: {theme_precision:.4f}")
    print(f"  Theme Recall: {theme_recall:.4f}")
    print(f"  Theme F1 Score: {theme_f1:.4f}")

    subtheme_accuracy, subtheme_precision, subtheme_recall, subtheme_f1 = get_score(true_subthemes, pred_subthemes)

    print(f"  Subtheme Accuracy: {subtheme_accuracy:.4f}")
    print(f"  Subtheme Precision: {subtheme_precision:.4f}")
    print(f"  Subtheme Recall: {subtheme_recall:.4f}")
    print(f"  Subtheme F1 Score: {subtheme_f1:.4f}")


    true_themes_str = ['\n'.join(x) for x in true_themes]
    pred_themes_str = ['\n'.join(x) for x in pred_themes]
    true_subthemes_str = ['\n'.join(str(x) if x == x else "" for x in sublist) for sublist in true_subthemes]
    pred_subthemes_str = ['\n'.join(str(x) if x == x else "" for x in sublist) for sublist in pred_subthemes]

    comparison_df = pd.DataFrame({'Text': text, 'Theme True': true_themes_str, 'Theme Pred': pred_themes_str,
                                  'Subtheme True': true_subthemes_str, 'Subtheme Pred': pred_subthemes_str})

    comparison_df.to_excel('results/label_comparison' + prompt_version + '.xlsx', index=False)

if __name__ == '__main__':
    prompt_version = ""

    dataset_one = 'data/DS1-LabelledData.xlsx'
    dataset_two = 'data/DS2-UnlabelledData.xlsx'

    print(">> Creating splits from labelled data.")
    create_splits(dataset_one)
    
    for use_unseen_data in [False, True]:
        if use_unseen_data:
            print(">> Processing unlabelled data.")
            train_splits = [pd.read_excel(dataset_one)]
            test_splits = [pd.read_excel(dataset_two)]
        else:
            print(">> Processing labelled data splits.")
            train_splits, test_splits = load_splits()

        call_gpt(train_splits, test_splits, prompt_version, use_unseen_data)

        if use_unseen_data == False:
            print(">> Evaluating predictions.")
            evaluate_splits(prompt_version)

    print(">> All tasks finished!")