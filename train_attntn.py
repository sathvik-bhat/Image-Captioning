import pickle
from torch.nn.utils.rnn import pack_padded_sequence
from torch.utils.data import DataLoader
from torchvision import transforms
import tqdm
import torch
import itertools
from datasets.flickr8k import Flickr8kDataset
from metrics import *
import numpy as np
import os


device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
device

DATASET_BASE_PATH = 'data/flickr8k/'


train_set = Flickr8kDataset(dataset_base_path=DATASET_BASE_PATH, dist='train', device=device,
                            return_type='tensor',
                            load_img_to_memory=False)
vocab, word2idx, idx2word, max_len = vocab_set = train_set.get_vocab()
val_set = Flickr8kDataset(dataset_base_path=DATASET_BASE_PATH, dist='val', vocab_set=vocab_set, device=device,
                          return_type='corpus',
                          load_img_to_memory=False)
test_set = Flickr8kDataset(dataset_base_path=DATASET_BASE_PATH, dist='test', vocab_set=vocab_set, device=device,
                           return_type='corpus',
                           load_img_to_memory=False)
train_eval_set = Flickr8kDataset(dataset_base_path=DATASET_BASE_PATH, dist='train', vocab_set=vocab_set, device=device,
                                 return_type='corpus',
                                 load_img_to_memory=False)
with open('vocab_set.pkl', 'wb') as f:
    pickle.dump(train_set.get_vocab(), f)
len(train_set), len(val_set), len(test_set)


vocab_size = len(vocab)
vocab_size, max_len


MODEL = "resnet101_attention"
EMBEDDING_DIM = 300
EMBEDDING = f"{EMBEDDING_DIM}"
ATTENTION_DIM = 256
DECODER_SIZE = 256
BATCH_SIZE = 128
LR = 1e-3
MODEL_NAME = f'saved_models/{MODEL}_b{BATCH_SIZE}_emd{EMBEDDING}'
NUM_EPOCHS = 2
SAVE_FREQ = 10
LOG_INTERVAL = 25 * (256 // BATCH_SIZE)

def glove_dictionary(GLOVE_DIR, dim=200):
    embeddings_index = {}
    f = open(os.path.join(GLOVE_DIR, f'glove.6B.{dim}d.txt'), encoding="utf8")
    for line in f:
        values = line.split()
        word = values[0]
        coefs = np.asarray(values[1:], dtype='float32')
        embeddings_index[word] = coefs
    f.close()
    return embeddings_index

def embedding_matrix_creator(embedding_dim, word2idx, GLOVE_DIR='data/glove.6B/'):
    embeddings_index = glove_dictionary(GLOVE_DIR=GLOVE_DIR, dim=embedding_dim)
    embedding_matrix = np.zeros((len(word2idx), embedding_dim))
    for word, i in tqdm(word2idx.items()):
        embedding_vector = embeddings_index.get(word.lower())
        if embedding_vector is not None:
            embedding_matrix[i] = embedding_vector
    return embedding_matrix

embedding_matrix = embedding_matrix_creator(embedding_dim=EMBEDDING_DIM, word2idx=word2idx)
embedding_matrix.shape

def words_from_tensors_fn(idx2word, max_len=40, startseq='<start>', endseq='<end>'):
    def words_from_tensors(captions: np.array) -> list:
        captoks = []
        for capidx in captions:
            captoks.append(list(itertools.takewhile(lambda word: word != endseq,
                                                    map(lambda idx: idx2word[idx], iter(capidx))))[1:])
        return captoks

    return words_from_tensors

def train_model(train_loader, model, loss_fn, optimizer, vocab_size, acc_fn, desc=''):
    running_acc = 0.0
    running_loss = 0.0
    model.train()
    t = tqdm(iter(train_loader), desc=f'{desc}')
    for batch_idx, batch in enumerate(t):
        images, captions, lengths = batch

        optimizer.zero_grad()

        scores, caps_sorted, decode_lengths, alphas, sort_ind = model(images, captions, lengths)

        targets = caps_sorted[:, 1:]

        scores = pack_padded_sequence(scores, decode_lengths, batch_first=True)[0]
        targets = pack_padded_sequence(targets, decode_lengths, batch_first=True)[0]

        loss = loss_fn(scores, targets)
        loss.backward()
        optimizer.step()

        running_acc += (torch.argmax(scores, dim=1) == targets).sum().float().item() / targets.size(0)
        running_loss += loss.item()
        t.set_postfix({'loss': running_loss / (batch_idx + 1),
                       'acc': running_acc / (batch_idx + 1),
                       }, refresh=True)
        if (batch_idx + 1) % LOG_INTERVAL == 0:
            print(f'{desc} {batch_idx + 1}/{len(train_loader)} '
                  f'train_loss: {running_loss / (batch_idx + 1):.4f} '
                  f'train_acc: {running_acc / (batch_idx + 1):.4f}')

    return running_loss / len(train_loader)


def evaluate_model(data_loader, model, loss_fn, vocab_size, bleu_score_fn, tensor_to_word_fn, desc=''):
    running_bleu = [0.0] * 5
    model.eval()
    t = tqdm(iter(data_loader), desc=f'{desc}')
    for batch_idx, batch in enumerate(t):
        images, captions, lengths = batch
        outputs = tensor_to_word_fn(model.sample(images).cpu().numpy())

        for i in (1, 2, 3, 4):
            running_bleu[i] += bleu_score_fn(reference_corpus=captions, candidate_corpus=outputs, n=i)
        t.set_postfix({
            'bleu1': running_bleu[1] / (batch_idx + 1),
            'bleu4': running_bleu[4] / (batch_idx + 1),
        }, refresh=True)
    for i in (1, 2, 3, 4):
        running_bleu[i] /= len(data_loader)
    return running_bleu


from models.torch.resnet101_attention import Captioner

final_model = Captioner(encoded_image_size=14, encoder_dim=2048,
                        attention_dim=ATTENTION_DIM, embed_dim=EMBEDDING_DIM, decoder_dim=DECODER_SIZE,
                        vocab_size=vocab_size,
                        embedding_matrix=embedding_matrix, train_embd=False).to(device)

loss_fn = torch.nn.CrossEntropyLoss(ignore_index=train_set.pad_value).to(device)
acc_fn = accuracy_fn(ignore_value=train_set.pad_value)
sentence_bleu_score_fn = bleu_score_fn(4, 'sentence')
corpus_bleu_score_fn = bleu_score_fn(4, 'corpus')
tensor_to_word_fn = words_from_tensors_fn(idx2word=idx2word)

params = final_model.parameters()

optimizer = torch.optim.RMSprop(params=params, lr=LR)

train_transformations = transforms.Compose([
    transforms.Resize(256),  # smaller edge of image resized to 256
    transforms.RandomCrop(256),  # get 256x256 crop from random location
    transforms.RandomHorizontalFlip(p=0.5),
    transforms.ToTensor(),  # convert the PIL Image to a tensor
    transforms.Normalize((0.485, 0.456, 0.406),  # normalize image for pre-trained model
                         (0.229, 0.224, 0.225))
])
eval_transformations = transforms.Compose([
    transforms.Resize(256),  # smaller edge of image resized to 256
    transforms.CenterCrop(256),  # get 256x256 crop from random location
    transforms.ToTensor(),  # convert the PIL Image to a tensor
    transforms.Normalize((0.485, 0.456, 0.406),  # normalize image for pre-trained model
                         (0.229, 0.224, 0.225))
])

train_set.transformations = train_transformations
val_set.transformations = eval_transformations
test_set.transformations = eval_transformations
train_eval_set.transformations = eval_transformations


eval_collate_fn = lambda batch: (torch.stack([x[0] for x in batch]), [x[1] for x in batch], [x[2] for x in batch])
train_loader = DataLoader(train_set, batch_size=BATCH_SIZE, shuffle=True, sampler=None, pin_memory=False)
val_loader = DataLoader(val_set, batch_size=BATCH_SIZE, shuffle=False, sampler=None, pin_memory=False,
                        collate_fn=eval_collate_fn)
test_loader = DataLoader(test_set, batch_size=BATCH_SIZE, shuffle=False, sampler=None, pin_memory=False,
                         collate_fn=eval_collate_fn)
train_eval_loader = DataLoader(train_eval_set, batch_size=BATCH_SIZE, shuffle=False, sampler=None, pin_memory=False,
                               collate_fn=eval_collate_fn)



train_loss_min = 100
val_bleu4_max = 0.0
for epoch in range(NUM_EPOCHS):
    train_loss = train_model(desc=f'Epoch {epoch + 1}/{NUM_EPOCHS}', model=final_model,
                             optimizer=optimizer, loss_fn=loss_fn, acc_fn=acc_fn,
                             train_loader=train_loader, vocab_size=vocab_size)
    with torch.no_grad():
        train_bleu = evaluate_model(desc=f'\tTrain Bleu Score: ', model=final_model,
                                    loss_fn=loss_fn, bleu_score_fn=corpus_bleu_score_fn,
                                    tensor_to_word_fn=tensor_to_word_fn,
                                    data_loader=train_eval_loader, vocab_size=vocab_size)
        val_bleu = evaluate_model(desc=f'\tValidation Bleu Score: ', model=final_model,
                                  loss_fn=loss_fn, bleu_score_fn=corpus_bleu_score_fn,
                                  tensor_to_word_fn=tensor_to_word_fn,
                                  data_loader=val_loader, vocab_size=vocab_size)
        print(f'Epoch {epoch + 1}/{NUM_EPOCHS}',
              ''.join([f'train_bleu{i}: {train_bleu[i]:.4f} ' for i in (1, 4)]),
              ''.join([f'val_bleu{i}: {val_bleu[i]:.4f} ' for i in (1, 4)]),
              )
        state = {
            'epoch': epoch + 1,
            'state_dict': final_model.state_dict(),
            'optimizer': optimizer.state_dict(),
            'train_loss_latest': train_loss,
            'val_bleu4_latest': val_bleu[4],
            'train_loss_min': min(train_loss, train_loss_min),
            'val_bleu4_max': max(val_bleu[4], val_bleu4_max),
            'train_bleus': train_bleu,
            'val_bleus': val_bleu,
        }
        torch.save(state, f'{MODEL_NAME}_latest.pt')
        if train_loss < train_loss_min:
            train_loss_min = train_loss
            torch.save(state, f'{MODEL_NAME}''_best_train.pt')
        if val_bleu[4] > val_bleu4_max:
            val_bleu4_max = val_bleu[4]
            torch.save(state, f'{MODEL_NAME}''_best_val.pt')

torch.save(state, f'{MODEL_NAME}_ep{NUM_EPOCHS:02d}_weights.pt')
final_model.eval()